import os
import re
import json
import math
from pathlib import Path
from datetime import datetime
from typing import Literal, Optional

import numpy as np
from fastapi import FastAPI, Depends, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse, PlainTextResponse, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session, load_only
from sqlalchemy import func, distinct

from database import (get_db, create_tables, WatchedPath, FitsFile, SavedQuery, Target,
                      get_config, set_config)
import pixels
import scanner
import expression as expr_engine
import sequence as seq_engine
import jobqueue
from logging_setup import setup_logging


# Expression/query variable name -> model column. Names are matched lowercased.
QUERY_VARS = {
    "hfr": FitsFile.median_hfr,
    "fwhm": FitsFile.median_fwhm,
    "fwhm_arcsec": FitsFile.fwhm_arcsec,
    "ecc": FitsFile.eccentricity,
    "eccentricity": FitsFile.eccentricity,
    "trail": FitsFile.trail_score,
    "trail_score": FitsFile.trail_score,
    "empty_cells": FitsFile.empty_cells,
    "bg_spread": FitsFile.bg_spread,
    "bg_dip": FitsFile.bg_dip,
    "psf_fwhm": FitsFile.psf_fwhm,
    "psf_beta": FitsFile.psf_beta,
    "beta": FitsFile.psf_beta,
    "star_flux": FitsFile.star_flux,
    "streaks": FitsFile.streaks,
    "stars": FitsFile.star_count,
    "starcount": FitsFile.star_count,
    "quality": FitsFile.quality_score,
    "background": FitsFile.background_median,
    "exptime": FitsFile.exptime,
    "ccd_temp": FitsFile.ccd_temp,
    "set_temp": FitsFile.set_temp,
    "focal": FitsFile.focal_length,
    "focal_length": FitsFile.focal_length,
    "pixscale": FitsFile.pixel_scale,
    "pixel_scale": FitsFile.pixel_scale,
    "ra": FitsFile.ra,                 # effective (astap-preferred) — overridden below
    "dec": FitsFile.dec,
    "acquisition_ra": FitsFile.ra,     # raw header / mount-reported
    "acquisition_dec": FitsFile.dec,
    "astap_ra": FitsFile.wcs_ra,       # raw plate-solved
    "astap_dec": FitsFile.wcs_dec,
    "coord_sep": FitsFile.coord_sep_arcmin,   # arcmin between the two
    "width": FitsFile.naxis1,
    "height": FitsFile.naxis2,
    "solved": FitsFile.solved,
    "wcs_scale": FitsFile.wcs_scale,
    "wcs_rotation": FitsFile.wcs_rotation,
    "altitude": FitsFile.altitude,
    "alt": FitsFile.altitude,
    "azimuth": FitsFile.azimuth,
    "airmass": FitsFile.airmass,
    "moonsep": FitsFile.moon_sep,
    "moon_sep": FitsFile.moon_sep,
    "moonillum": FitsFile.moon_illum,
    "moon_illum": FitsFile.moon_illum,
    "moonalt": FitsFile.moon_alt,
    "moon_alt": FitsFile.moon_alt,
}


def get_site_default(db: Session) -> Optional[dict]:
    """Read the configured default observing site, if any."""
    lat = get_config(db, "site_lat")
    lon = get_config(db, "site_lon")
    if lat is None or lon is None:
        return None
    try:
        return {
            "lat": float(lat),
            "lon": float(lon),
            "elev": float(get_config(db, "site_elev") or 0),
        }
    except (TypeError, ValueError):
        return None

# ── Paths ────────────────────────────────────────────────────────────────────
import config
THUMBNAILS_DIR = config.THUMBNAILS_DIR

# ── App ──────────────────────────────────────────────────────────────────────
app = FastAPI(title="fitsql", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


import logging
log = logging.getLogger("fitsql.web")


@app.on_event("startup")
def on_startup():
    setup_logging()
    THUMBNAILS_DIR.mkdir(parents=True, exist_ok=True)
    config.PREVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    config.ASTAP_DB_DIR.mkdir(parents=True, exist_ok=True)
    create_tables()
    _migrate_imagetyp()
    _seed_from_env()
    log.info("web ready — redis=%s, queue available=%s", config.REDIS_URL, jobqueue.available())


@app.middleware("http")
async def _timing(request, call_next):
    import time
    t0 = time.time()
    response = await call_next(request)
    ms = (time.time() - t0) * 1000.0
    if ms >= config.SLOW_REQUEST_MS:
        log.warning("slow request %.0fms %s %s", ms, request.method, request.url.path)
    response.headers["X-Response-Time-ms"] = f"{ms:.0f}"
    return response


def _seed_from_env():
    """Seed config + watched paths from environment on first boot (idempotent)."""
    db = next(get_db())
    try:
        # Default config values, only if not already set in the DB
        seeds = {
            "site_lat": config.SITE_LAT, "site_lon": config.SITE_LON,
            "site_elev": config.SITE_ELEV, "astap_path": config.ASTAP_PATH,
            "export_map_from": config.EXPORT_MAP_FROM, "export_map_to": config.EXPORT_MAP_TO,
        }
        for key, val in seeds.items():
            if val is not None and get_config(db, key) is None:
                set_config(db, key, str(val))

        # Auto-add watched paths from env that aren't already present
        for p in config.WATCH_PATHS:
            if not db.query(WatchedPath).filter(WatchedPath.path == p).first():
                db.add(WatchedPath(path=p, created_at=datetime.utcnow()))
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


def get_astap_path(db: Session) -> Optional[str]:
    return get_config(db, "astap_path") or config.ASTAP_PATH


def _migrate_imagetyp():
    """One-time normalization of imagetyp + filter values already in DB."""
    db = next(get_db())
    try:
        rows = db.query(FitsFile.id, FitsFile.imagetyp).filter(FitsFile.imagetyp.isnot(None)).all()
        for row_id, raw in rows:
            normalized = scanner._normalize_imagetyp(raw)
            if normalized != raw:
                db.query(FitsFile).filter(FitsFile.id == row_id).update({"imagetyp": normalized})
        # Filters: update per distinct value (few values, many rows)
        raws = [r[0] for r in db.query(distinct(FitsFile.filter)).filter(FitsFile.filter.isnot(None)).all()]
        for raw in raws:
            norm = scanner._normalize_filter(raw)
            if norm != raw:
                db.query(FitsFile).filter(FitsFile.filter == raw).update({"filter": norm})
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


# Serve thumbnails as static files
app.mount("/thumbnails", StaticFiles(directory=str(THUMBNAILS_DIR)), name="thumbnails")


# ── Schemas ───────────────────────────────────────────────────────────────────
class PathCreate(BaseModel):
    path: str


# ── Helpers ───────────────────────────────────────────────────────────────────
# `rejected` column <-> grade: 1 = rejected, 0 = accepted, null = unmarked
_GRADE_NAME = {1: "rejected", 0: "accepted"}
_GRADE_VALUE = {"rejected": 1, "accepted": 0, None: None}


def _trailed(trail_score) -> Optional[bool]:
    """Trailing verdict against the configured threshold; None until the frame is analyzed."""
    return None if trail_score is None else bool(trail_score > config.TRAIL_THRESHOLD)


def file_to_dict(f: FitsFile) -> dict:
    return {
        "id": f.id,
        "path": f.path,
        "filename": f.filename,
        "watched_path_id": f.watched_path_id,
        "file_size": f.file_size,
        "indexed_at": f.indexed_at.isoformat() if f.indexed_at else None,
        "thumbnail": f.thumbnail,
        "thumbnail_url": f"/thumbnails/{f.thumbnail}" if f.thumbnail else None,
        "star_count": f.star_count,
        "median_hfr": f.median_hfr,
        "median_fwhm": f.median_fwhm,
        "fwhm_arcsec": f.fwhm_arcsec,
        "eccentricity": f.eccentricity,
        "trail_score": f.trail_score,
        "trailed": _trailed(f.trail_score),
        "background_median": f.background_median,
        "pixel_scale": f.pixel_scale,
        "quality_score": f.quality_score,
        "empty_cells": f.empty_cells,
        "bg_spread": f.bg_spread,
        "bg_dip": f.bg_dip,
        "shadow": None if f.bg_dip is None else bool(f.bg_dip > config.SHADOW_THRESHOLD),
        "psf_fwhm": f.psf_fwhm,
        "psf_beta": f.psf_beta,
        "star_flux": f.star_flux,
        "streaks": f.streaks,
        "altitude": f.altitude,
        "azimuth": f.azimuth,
        "airmass": f.airmass,
        "moon_sep": f.moon_sep,
        "moon_illum": f.moon_illum,
        "moon_alt": f.moon_alt,
        "solved": f.solved,
        "wcs_ra": f.wcs_ra,
        "wcs_dec": f.wcs_dec,
        "wcs_scale": f.wcs_scale,
        "wcs_rotation": f.wcs_rotation,
        "fov_w": f.fov_w,
        "fov_h": f.fov_h,
        "coord_sep_arcmin": f.coord_sep_arcmin,
        "bayer": f.bayer,
        "rejected": f.rejected,
        "grade": _GRADE_NAME.get(f.rejected),
        "index_error": f.index_error,
        "object": f.object,
        "imagetyp": f.imagetyp,
        "date_obs": f.date_obs,
        "exptime": f.exptime,
        "focal_length": f.focal_length,
        "telescop": f.telescop,
        "instrume": f.instrume,
        "filter": f.filter,
        "ra": f.ra,
        "dec": f.dec,
        "gain": f.gain,
        "offset": f.offset,
        "ccd_temp": f.ccd_temp,
        "set_temp": f.set_temp,
        "xbinning": f.xbinning,
        "ybinning": f.ybinning,
        "naxis1": f.naxis1,
        "naxis2": f.naxis2,
        "header_json": f.header_json,
    }


# ── Routes: Directory browser ─────────────────────────────────────────────────
_FITS_EXTS = tuple(scanner.FITS_EXTENSIONS)


@app.get("/api/browse")
def browse(path: Optional[str] = Query(None)):
    """List subdirectories under the allowed browse roots, for the folder picker."""
    roots = [os.path.realpath(r) for r in config.BROWSE_ROOTS]

    if not path:
        # Top level: show the configured roots that exist
        dirs = [{"name": r, "path": r} for r in roots if os.path.isdir(r)]
        return {"path": None, "parent": None, "dirs": dirs, "has_fits": False}

    real = os.path.realpath(path)
    if not any(real == r or real.startswith(r + os.sep) for r in roots):
        raise HTTPException(status_code=403, detail="Path is outside the allowed roots")
    if not os.path.isdir(real):
        raise HTTPException(status_code=404, detail="Not a directory")

    dirs, has_fits = [], False
    try:
        with os.scandir(real) as it:
            for e in it:
                if e.name.startswith("."):
                    continue
                if e.is_dir(follow_symlinks=False):
                    dirs.append({"name": e.name, "path": os.path.join(real, e.name)})
                elif not has_fits and os.path.splitext(e.name)[1].lower() in _FITS_EXTS:
                    has_fits = True
    except PermissionError:
        raise HTTPException(status_code=403, detail="Permission denied")

    dirs.sort(key=lambda d: d["name"].lower())

    # Offer an "up" target only while it stays within a root
    parent = os.path.dirname(real)
    at_root = any(real == r for r in roots)
    if at_root or not any(parent == r or parent.startswith(r + os.sep) for r in roots):
        parent = None
    return {"path": real, "parent": parent, "dirs": dirs, "has_fits": has_fits}


# ── Routes: Paths ─────────────────────────────────────────────────────────────
@app.get("/api/paths")
def list_paths(db: Session = Depends(get_db)):
    paths = db.query(WatchedPath).order_by(WatchedPath.created_at).all()
    return [
        {"id": p.id, "path": p.path, "created_at": p.created_at.isoformat() if p.created_at else None}
        for p in paths
    ]


@app.post("/api/paths", status_code=201)
def add_path(body: PathCreate, db: Session = Depends(get_db)):
    path_str = body.path.strip()
    if not path_str:
        raise HTTPException(status_code=400, detail="Path cannot be empty")

    existing = db.query(WatchedPath).filter(WatchedPath.path == path_str).first()
    if existing:
        raise HTTPException(status_code=409, detail="Path already exists")

    wp = WatchedPath(path=path_str, created_at=datetime.utcnow())
    db.add(wp)
    db.commit()
    db.refresh(wp)
    return {"id": wp.id, "path": wp.path, "created_at": wp.created_at.isoformat()}


@app.delete("/api/paths/{path_id}", status_code=204)
def delete_path(path_id: int, db: Session = Depends(get_db)):
    wp = db.query(WatchedPath).filter(WatchedPath.id == path_id).first()
    if not wp:
        raise HTTPException(status_code=404, detail="Path not found")
    db.delete(wp)
    db.commit()
    return None


# ── Routes: Jobs (enqueue to the worker queue) ────────────────────────────────
def _require_queue():
    if not jobqueue.available():
        raise HTTPException(status_code=503,
                            detail="Job queue (Redis) unavailable. Is the redis service running?")


@app.post("/api/scan")
def trigger_scan(db: Session = Depends(get_db)):
    """Enqueue a scan job per watched path. Returns immediately; workers do the work."""
    _require_queue()
    paths = db.query(WatchedPath).all()
    if not paths:
        return {"enqueued": 0, "message": "No watched paths configured"}
    jobs = [{"op": "scan", "path": p.path, "watched_path_id": p.id} for p in paths]
    jobqueue.enqueue(jobs)
    return {"enqueued": len(jobs), "queue": jobqueue.status()}


@app.post("/api/paths/{path_id}/scan")
def trigger_scan_one(path_id: int, db: Session = Depends(get_db)):
    """Scan a single watched path — only that dir is re-walked for new/changed files."""
    _require_queue()
    p = db.query(WatchedPath).filter(WatchedPath.id == path_id).first()
    if not p:
        raise HTTPException(status_code=404, detail="Path not found")
    jobqueue.enqueue_one({"op": "scan", "path": p.path, "watched_path_id": p.id})
    return {"enqueued": 1, "path": p.path}


@app.post("/api/reanalyze")
def trigger_reanalyze(only_missing: bool = Query(True)):
    """Enqueue a reanalyze expansion job (worker fans it out per row)."""
    _require_queue()
    jobqueue.enqueue_one({"op": "reanalyze_all", "only_missing": only_missing})
    return {"queued": True, "queue": jobqueue.status()}


@app.post("/api/platesolve")
def trigger_platesolve(only_unsolved: bool = Query(True), db: Session = Depends(get_db)):
    _require_queue()
    status = _astap_status(db)
    if not status["available"]:
        raise HTTPException(status_code=400,
                            detail="ASTAP not ready — set the binary path and download a star database first.")
    jobqueue.enqueue_one({"op": "solve_all", "only_unsolved": only_unsolved})
    return {"queued": True, "queue": jobqueue.status()}


# ── Routes: Queue status / control ────────────────────────────────────────────
@app.get("/api/queue")
def queue_status():
    st = jobqueue.status()
    if st.get("available"):
        st["sample"] = jobqueue.peek(15)
    return st


@app.post("/api/queue/flush")
def queue_flush():
    _require_queue()
    return jobqueue.flush()


# ── Routes: Benchmark ─────────────────────────────────────────────────────────
@app.post("/api/benchmark/pick")
def benchmark_pick(n: int = Query(10, ge=1, le=50), db: Session = Depends(get_db)):
    import benchmark
    files = benchmark.pick_files(db, n)
    return {"files": files, "count": len(files)}


@app.post("/api/benchmark/run")
def benchmark_run(repeats: int = Query(1, ge=1, le=5),
                  dtypes: str = Query("float32,float64"), db: Session = Depends(get_db)):
    import benchmark
    _require_queue()
    files = benchmark.get_files(db)
    if not files:
        raise HTTPException(status_code=400, detail="No benchmark files selected — pick some first.")
    dt = [d.strip() for d in dtypes.split(",") if d.strip() in benchmark.DTYPES] or benchmark.DTYPES
    jobqueue.enqueue_one({"op": "benchmark", "files": files, "dtypes": dt, "repeats": repeats})
    return {"queued": True, "files": len(files), "dtypes": dt}


@app.get("/api/benchmark")
def benchmark_status(db: Session = Depends(get_db)):
    import json
    import benchmark
    files = benchmark.get_files(db)
    progress = result = None
    if jobqueue.available():
        try:
            r = jobqueue.client()
            p = r.get(benchmark.PROGRESS_KEY)
            res = r.get(benchmark.RESULT_KEY)
            progress = json.loads(p) if p else None
            result = json.loads(res) if res else None
        except Exception:
            pass
    return {"files": files, "phases": benchmark.PHASES, "progress": progress, "result": result}


# ── Routes: Files ─────────────────────────────────────────────────────────────
def _norm_path_col():
    """FitsFile.path with separators folded to '/', so patterns work on any OS."""
    return func.replace(FitsFile.path, "\\", "/")


_END_PAD = {4: "-12-31T23:59:59.999999999", 7: "-31T23:59:59.999999999", 10: "T23:59:59.999999999",
            13: ":59:59.999999999", 16: ":59.999999999", 19: ".999999999"}


def _date_bound(v: str, end: bool = False) -> str:
    """ISO bound for string compares on date_obs (UTC). Accepts 'YYYY-MM-DD' with an
    optional ' HH:MM[:SS]'; an end bound is inclusive at the precision typed."""
    v = v.strip().upper().replace(" ", "T")
    return v + _END_PAD.get(len(v), "") if end else v


def _path_pattern(v: str) -> str:
    """SQL LIKE pattern for a path filter; plain text (no %) means 'contains'."""
    v = v.strip().replace("\\", "/")
    return v if "%" in v else f"%{v}%"


@app.get("/api/files")
def list_files(
    object: Optional[str] = Query(None),
    filter: Optional[str] = Query(None),
    imagetyp: Optional[str] = Query(None),
    telescop: Optional[str] = Query(None),
    instrume: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    focal_min: Optional[float] = Query(None),
    focal_max: Optional[float] = Query(None),
    exptime_min: Optional[float] = Query(None),
    exptime_max: Optional[float] = Query(None),
    hfr_max: Optional[float] = Query(None),
    ecc_max: Optional[float] = Query(None),
    stars_min: Optional[int] = Query(None),
    tracking: Optional[str] = Query(None, description="'good' = trail_score at/below threshold, 'trailed' = above"),
    path_like: Optional[str] = Query(None),
    path_not_like: Optional[str] = Query(None),
    failed: bool = Query(False),
    sort: str = Query("date_obs"),
    order: str = Query("desc"),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    q = db.query(FitsFile)
    if failed:
        q = q.filter(FitsFile.index_error.isnot(None))
    else:
        q = q.filter(FitsFile.index_error.is_(None))   # hide tombstones from normal listing

    if object:
        q = q.filter(FitsFile.object.ilike(f"%{object}%"))
    if filter:
        q = q.filter(FitsFile.filter == filter)
    if imagetyp:
        q = q.filter(FitsFile.imagetyp == imagetyp)
    if telescop:
        q = q.filter(FitsFile.telescop == telescop)
    if instrume:
        q = q.filter(FitsFile.instrume == instrume)
    if date_from:
        q = q.filter(FitsFile.date_obs >= _date_bound(date_from))
    if date_to:
        q = q.filter(FitsFile.date_obs <= _date_bound(date_to, end=True))
    if focal_min is not None:
        q = q.filter(FitsFile.focal_length >= focal_min)
    if focal_max is not None:
        q = q.filter(FitsFile.focal_length <= focal_max)
    if exptime_min is not None:
        q = q.filter(FitsFile.exptime >= exptime_min)
    if exptime_max is not None:
        q = q.filter(FitsFile.exptime <= exptime_max)
    if hfr_max is not None:
        q = q.filter(FitsFile.median_hfr <= hfr_max)
    if ecc_max is not None:
        q = q.filter(FitsFile.eccentricity <= ecc_max)
    if stars_min is not None:
        q = q.filter(FitsFile.star_count >= stars_min)
    # Not-yet-analyzed frames (NULL trail_score) match neither, like the selector
    if tracking == "good":
        q = q.filter(FitsFile.trail_score <= config.TRAIL_THRESHOLD)
    elif tracking == "trailed":
        q = q.filter(FitsFile.trail_score > config.TRAIL_THRESHOLD)
    if path_like and path_like.strip():
        q = q.filter(_norm_path_col().ilike(_path_pattern(path_like)))
    if path_not_like and path_not_like.strip():
        q = q.filter(~_norm_path_col().ilike(_path_pattern(path_not_like)))

    total = q.count()
    pages = math.ceil(total / per_page) if total > 0 else 1
    offset = (page - 1) * per_page

    # ── Sorting ──
    SORTABLE = {
        "date_obs": FitsFile.date_obs, "exptime": FitsFile.exptime,
        "star_count": FitsFile.star_count, "median_hfr": FitsFile.median_hfr,
        "median_fwhm": FitsFile.median_fwhm, "eccentricity": FitsFile.eccentricity,
        "trail_score": FitsFile.trail_score,
        "quality_score": FitsFile.quality_score, "filename": FitsFile.filename,
        "focal_length": FitsFile.focal_length, "indexed_at": FitsFile.indexed_at,
    }
    sort_col = SORTABLE.get(sort, FitsFile.date_obs)
    # NULLs last regardless of direction (bad/unmeasured frames shouldn't top the list)
    direction = sort_col.desc() if order == "desc" else sort_col.asc()

    items = q.options(load_only(
        FitsFile.id, FitsFile.path, FitsFile.filename, FitsFile.watched_path_id,
        FitsFile.file_size, FitsFile.indexed_at, FitsFile.thumbnail,
        FitsFile.star_count, FitsFile.median_hfr, FitsFile.median_fwhm,
        FitsFile.fwhm_arcsec, FitsFile.eccentricity, FitsFile.trail_score,
        FitsFile.empty_cells, FitsFile.bg_spread, FitsFile.bg_dip, FitsFile.psf_fwhm, FitsFile.psf_beta,
        FitsFile.star_flux, FitsFile.streaks, FitsFile.background_median,
        FitsFile.pixel_scale, FitsFile.quality_score,
        FitsFile.altitude, FitsFile.azimuth, FitsFile.airmass,
        FitsFile.moon_sep, FitsFile.moon_illum, FitsFile.moon_alt,
        FitsFile.solved, FitsFile.wcs_ra, FitsFile.wcs_dec, FitsFile.wcs_scale,
        FitsFile.wcs_rotation, FitsFile.fov_w, FitsFile.fov_h, FitsFile.coord_sep_arcmin,
        FitsFile.bayer, FitsFile.rejected, FitsFile.index_error,
        FitsFile.object, FitsFile.imagetyp,
        FitsFile.date_obs, FitsFile.exptime, FitsFile.focal_length,
        FitsFile.telescop, FitsFile.instrume, FitsFile.filter,
        FitsFile.ra, FitsFile.dec, FitsFile.gain, FitsFile.offset,
        FitsFile.ccd_temp, FitsFile.set_temp, FitsFile.xbinning, FitsFile.ybinning,
        FitsFile.naxis1, FitsFile.naxis2,
    )).order_by((sort_col.is_(None)).asc(), direction, FitsFile.indexed_at.desc()) \
      .offset(offset).limit(per_page).all()

    def _list_dict(f):
        d = file_to_dict(f)
        d.pop("header_json", None)
        return d

    return {
        "items": [_list_dict(f) for f in items],
        "total": total,
        "page": page,
        "pages": pages,
        "per_page": per_page,
    }


@app.get("/api/files/{file_id}")
def get_file(file_id: int, db: Session = Depends(get_db)):
    f = db.query(FitsFile).filter(FitsFile.id == file_id).first()
    if not f:
        raise HTTPException(status_code=404, detail="File not found")
    return file_to_dict(f)


@app.post("/api/files/{file_id}/solve")
def solve_one_file(file_id: int, db: Session = Depends(get_db)):
    """Enqueue an ASTAP solve for a single file (from the detail popup)."""
    _require_queue()
    if not db.query(FitsFile.id).filter(FitsFile.id == file_id).first():
        raise HTTPException(status_code=404, detail="File not found")
    if not _astap_status(db)["available"]:
        raise HTTPException(status_code=400, detail="ASTAP not ready — set the binary path and a star database first.")
    jobqueue.enqueue_one({"op": "solve", "id": file_id})
    return {"queued": True}


@app.get("/api/files/{file_id}/diagnostics")
def get_diagnostics(file_id: int, db: Session = Depends(get_db)):
    """Per-star codes and grid cells recorded during analysis (preview overlay)."""
    row = db.query(FitsFile.diagnostics_json).filter(FitsFile.id == file_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="File not found")
    if not row[0]:
        raise HTTPException(status_code=404, detail="No diagnostics yet — run Re-analyze")
    return JSONResponse(content=json.loads(row[0]))


class PreviewError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def cached_preview(file_id: int, size: int, db: Session):
    """Path of the stretched JPEG preview (≤ `size` px, capped at PREVIEW_MAX_PX),
    rendered from the source file on first request. Shared with /api/v1."""
    row = db.query(FitsFile.path, FitsFile.file_mtime).filter(FitsFile.id == file_id).first()
    if row is None:
        raise PreviewError(404, "not_found", "File not found")
    size = min(size, config.PREVIEW_MAX_PX)
    name = f"{file_id}-{size}-{int(row.file_mtime or 0)}.jpg"
    out = config.PREVIEWS_DIR / name
    if not out.exists():
        if not os.path.exists(row.path):
            raise PreviewError(404, "source_unavailable", "Source file not available")
        config.PREVIEWS_DIR.mkdir(parents=True, exist_ok=True)
        if not scanner.render_preview(row.path, str(config.PREVIEWS_DIR), name, size):
            raise PreviewError(500, "render_failed", "Preview rendering failed")
    return out


@app.get("/api/files/{file_id}/preview")
def file_preview(file_id: int, size: int = Query(2048, ge=256, le=8192), db: Session = Depends(get_db)):
    """Stretched JPEG up to `size` px (capped at PREVIEW_MAX_PX) for zooming and
    comparing. Rendered from the FITS on first request, then served from cache."""
    try:
        out = cached_preview(file_id, size, db)
    except PreviewError as e:
        raise HTTPException(status_code=e.status, detail=e.message)
    return FileResponse(out, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


def _peep_frame(file_id: int, db: Session):
    """(row, frame) — the frame stretched at full resolution for the 100% viewer, read
    from the source file once and then served from memory (see pixels.py)."""
    row = db.query(FitsFile.path, FitsFile.file_mtime, FitsFile.bayer).filter(FitsFile.id == file_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="File not found")
    if not os.path.exists(row.path):
        raise HTTPException(status_code=404, detail="Source file not available")
    try:
        return row, pixels.frame((file_id, row.path, row.file_mtime), row.path, row.bayer)
    except Exception as e:
        log.warning("full-resolution render failed for %s: %s", row.path, e)
        raise HTTPException(status_code=500, detail="Could not read the frame at full resolution")


@app.get("/api/files/{file_id}/pixels")
def file_pixels(file_id: int, db: Session = Depends(get_db)):
    """Full-resolution size of a frame for the 100% viewer. The first call reads and
    stretches the whole frame (a few seconds for a large sub); tiles are fast after it."""
    row, frame = _peep_frame(file_id, db)
    return {"width": int(frame.shape[1]), "height": int(frame.shape[0]),
            "channels": 1 if frame.ndim == 2 else int(frame.shape[2]),
            "cfa": scanner.cfa_pattern(row.bayer) if frame.ndim == 2 else None}


@app.get("/api/files/{file_id}/tile")
def file_tile(file_id: int, x: int = Query(0, ge=0), y: int = Query(0, ge=0),
              w: int = Query(512, ge=1, le=2048), h: int = Query(512, ge=1, le=2048),
              db: Session = Depends(get_db)):
    """Lossless PNG of the native pixels in [x, x + w) × [y, y + h), clipped to the frame
    and stretched like the whole frame — pixel peeping at 100%."""
    _, frame = _peep_frame(file_id, db)
    if x >= frame.shape[1] or y >= frame.shape[0]:
        raise HTTPException(status_code=404, detail="Outside the frame")
    return Response(pixels.png(frame[y:y + h, x:x + w]), media_type="image/png",
                    headers={"Cache-Control": "private, max-age=3600"})


class RejectBody(BaseModel):
    rejected: bool = True


@app.post("/api/files/{file_id}/reject")
def set_reject(file_id: int, body: RejectBody, db: Session = Depends(get_db)):
    """Manually flag/unflag a frame as rejected (excluded via the selector).
    Kept for compatibility; un-rejecting clears the grade. See /grade."""
    return set_grade(file_id, GradeBody(grade="rejected" if body.rejected else None), db)


class GradeBody(BaseModel):
    grade: Optional[Literal["accepted", "rejected"]] = None   # null = unmark


@app.post("/api/files/{file_id}/grade")
def set_grade(file_id: int, body: GradeBody, db: Session = Depends(get_db)):
    """Grade a frame: accepted, rejected, or null to clear. Selector vars
    `accepted` / `rejected` read it; rejected frames are what `!rejected` drops."""
    f = db.query(FitsFile).filter(FitsFile.id == file_id).first()
    if not f:
        raise HTTPException(status_code=404, detail="File not found")
    f.rejected = _GRADE_VALUE[body.grade]
    db.commit()
    return {"id": file_id, "rejected": f.rejected, "grade": body.grade}


# ── Routes: Site config ───────────────────────────────────────────────────────
class SiteConfig(BaseModel):
    lat: Optional[float] = None
    lon: Optional[float] = None
    elev: Optional[float] = None


@app.get("/api/config/site")
def read_site(db: Session = Depends(get_db)):
    s = get_site_default(db)
    return s or {"lat": None, "lon": None, "elev": None}


@app.put("/api/config/site")
def write_site(body: SiteConfig, db: Session = Depends(get_db)):
    set_config(db, "site_lat", str(body.lat) if body.lat is not None else None)
    set_config(db, "site_lon", str(body.lon) if body.lon is not None else None)
    set_config(db, "site_elev", str(body.elev) if body.elev is not None else None)
    return {"ok": True, "site": get_site_default(db)}


class AstapConfig(BaseModel):
    path: Optional[str] = None


def _astap_status(db):
    import solver
    import astap_db
    path = get_astap_path(db)
    ready = astap_db.db_ready(config.ASTAP_DB_DIR)
    return {
        "path": path,
        "available": solver.astap_available(path) and ready,
        "solver_found": solver.astap_available(path),
        "db_ready": ready,
    }


@app.get("/api/astap/databases")
def astap_databases():
    import astap_db
    return astap_db.list_databases(config.ASTAP_DB_DIR)


@app.get("/api/astap/download/stream")
def astap_download_stream(db: str = Query(...), url: Optional[str] = Query(None)):
    import astap_db

    def generate():
        for event in astap_db.download_stream(config.ASTAP_DB_DIR, db, url):
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/config/astap")
def read_astap(db: Session = Depends(get_db)):
    return _astap_status(db)


@app.put("/api/config/astap")
def write_astap(body: AstapConfig, db: Session = Depends(get_db)):
    set_config(db, "astap_path", (body.path or "").strip() or None)
    return {"ok": True, **_astap_status(db)}


class ExportMap(BaseModel):
    src: Optional[str] = None
    dst: Optional[str] = None


@app.get("/api/config/export-map")
def read_export_map(db: Session = Depends(get_db)):
    return {
        "src": get_config(db, "export_map_from") or config.EXPORT_MAP_FROM,
        "dst": get_config(db, "export_map_to") or config.EXPORT_MAP_TO,
    }


@app.put("/api/config/export-map")
def write_export_map(body: ExportMap, db: Session = Depends(get_db)):
    set_config(db, "export_map_from", (body.src or "").strip() or None)
    set_config(db, "export_map_to", (body.dst or "").strip() or None)
    return {"ok": True, "src": get_config(db, "export_map_from"), "dst": get_config(db, "export_map_to")}


# ── Routes: Selector (expression query + export) ──────────────────────────────
class QueryRequest(BaseModel):
    expression: str = ""
    imagetyp: Optional[str] = "Light Frame"
    object: Optional[str] = None
    filter: Optional[str] = None
    telescop: Optional[str] = None
    instrume: Optional[str] = None
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    page: int = 1
    per_page: int = 50


# Friendly stat columns surfaced in the query summary
_STAT_COLS = {
    "hfr": FitsFile.median_hfr, "fwhm": FitsFile.median_fwhm,
    "eccentricity": FitsFile.eccentricity, "stars": FitsFile.star_count,
    "quality": FitsFile.quality_score, "altitude": FitsFile.altitude,
    "airmass": FitsFile.airmass, "moon_sep": FitsFile.moon_sep,
    "moon_illum": FitsFile.moon_illum, "background": FitsFile.background_median,
    "pixel_scale": FitsFile.pixel_scale, "focal": FitsFile.focal_length,
    "empty_cells": FitsFile.empty_cells, "bg_spread": FitsFile.bg_spread, "bg_dip": FitsFile.bg_dip,
    "psf_fwhm": FitsFile.psf_fwhm, "psf_beta": FitsFile.psf_beta, "star_flux": FitsFile.star_flux,
}


def _candidate_query(db, body: QueryRequest):
    q = db.query(FitsFile)
    if body.imagetyp:
        q = q.filter(FitsFile.imagetyp == body.imagetyp)
    if body.object:
        q = q.filter(FitsFile.object.ilike(f"%{body.object}%"))
    if body.filter:
        q = q.filter(FitsFile.filter == body.filter)
    if body.telescop:
        q = q.filter(FitsFile.telescop == body.telescop)
    if body.instrume:
        q = q.filter(FitsFile.instrume == body.instrume)
    if body.date_from:
        q = q.filter(FitsFile.date_obs >= _date_bound(body.date_from))
    if body.date_to:
        q = q.filter(FitsFile.date_obs <= _date_bound(body.date_to, end=True))
    return q


def _obs_datetime(s):
    if not s:
        return np.datetime64("NaT", "ms")
    try:
        return expr_engine.to_datetime64(s).astype("datetime64[ms]")
    except ValueError:
        return np.datetime64("NaT", "ms")


# Sequence screening is a per-session pass over all candidates; only run it when
# the expression uses one of its variables.
_SEQ_VARS = re.compile(
    r"\b(star_drop|hfr_rise|empty_rise|spread_rise|flux_drop|dip_rise|transparency|seq_anomaly|seq_ok)\b")


def _sequence_vars(candidates, dt, variables):
    """Sequence-screening arrays (see sequence.py). Sessions are one target /
    filter / rig / exposure; the lowercased object and filter arrays are reused."""
    keys = [(obj, flt, c.instrume, c.telescop, round(float(c.exptime), 1) if c.exptime is not None else None)
            for obj, flt, c in zip(variables["object"], variables["filter"], candidates)]
    out = seq_engine.screen(
        keys, dt, variables["stars"], variables["hfr"], variables["empty_cells"], variables["bg_spread"],
        flux=variables["star_flux"], dip=variables.get("bg_dip"),
        gap_hours=config.SEQ_GAP_HOURS, baseline_frames=config.SEQ_BASELINE_FRAMES,
        star_drop=config.SEQ_STAR_DROP, hfr_rise=config.SEQ_HFR_RISE,
        empty_rise=config.SEQ_EMPTY_RISE, spread_rise=config.SEQ_SPREAD_RISE,
        flux_drop=config.SEQ_FLUX_DROP, dip_rise=config.SEQ_DIP_RISE)
    # Bright-star flux relative to the session reference, 1 = as clear as it gets
    out["transparency"] = 1.0 - out["flux_drop"]
    return out


def _evaluate_candidates(candidates, expression: str):
    """Return boolean approved mask for the candidate list. Empty expr => all True."""
    n = len(candidates)
    if not expression.strip():
        return np.ones(n, dtype=bool)

    def col_array(attr):
        return np.array(
            [getattr(c, attr) if getattr(c, attr) is not None else np.nan for c in candidates],
            dtype=float,
        )

    # Per-frame variable arrays (None -> NaN)
    variables = {alias: col_array(col.key) for alias, col in QUERY_VARS.items()}

    # String columns (lowercased; the expression is lowercased too -> case-insensitive
    # equality, e.g. filter == 'h', object == 'ngc 7023', imagetyp == 'light frame')
    def str_array(attr):
        return np.array([(getattr(c, attr) or "").lower() for c in candidates], dtype=object)
    for name in ("filter", "imagetyp", "object", "telescop", "instrume", "bayer", "filename"):
        variables[name] = str_array(name)
    # Path with separators normalized to "/" so one pattern matches container and
    # Windows paths, e.g. path not like '%/reject%/%'
    variables["path"] = np.array(
        [(c.path or "").replace("\\", "/").lower() for c in candidates], dtype=object)

    # Capture time (DATE-OBS, UTC) as datetime64 (NaT when missing/unparseable),
    # e.g. date between '2026-05-01' and '2026-05-31', plus fractional UTC hour of day
    dt = np.array([_obs_datetime(c.date_obs) for c in candidates], dtype="datetime64[ms]")
    variables["date"] = variables["date_obs"] = dt
    variables["hour"] = np.where(
        np.isnat(dt), np.nan,
        (dt - dt.astype("datetime64[D]")).astype("timedelta64[ms]").astype(float) / 3_600_000.0)

    # Convenience booleans
    it = variables["imagetyp"]
    variables["light"] = (it == "light frame")
    variables["dark"] = (it == "dark frame")
    variables["flat"] = (it == "flat frame")
    variables["bias"] = (it == "bias frame")
    has_bayer = np.array([bool(getattr(c, "bayer")) for c in candidates])
    variables["osc"] = has_bayer
    variables["mono"] = ~has_bayer
    variables["rejected"] = np.array([bool(getattr(c, "rejected")) for c in candidates])
    # Tracking: trail_score above the configured threshold = stars smeared one way.
    # Not-yet-analyzed frames are neither, like any other missing metric.
    with np.errstate(invalid="ignore"):
        variables["trailed"] = variables["trail_score"] > config.TRAIL_THRESHOLD
        variables["good_tracking"] = variables["trail_score"] <= config.TRAIL_THRESHOLD
        # Grid screening: too many (nearly) starless cells = something blocks part of the field
        variables["occluded"] = variables["empty_cells"] > config.OCCLUDED_THRESHOLD
        variables["satellite"] = variables["streaks"] > 0
        # A soft dark patch in the background: frost or dew on the sensor window / a filter
        variables["shadow"] = variables["bg_dip"] > config.SHADOW_THRESHOLD
        # Sensor away from its setpoint: still cooling, cooler maxed out, setpoint changing
        variables["temp_delta"] = variables["ccd_temp"] - variables["set_temp"]
        variables["off_setpoint"] = np.abs(variables["temp_delta"]) > config.SETPOINT_TOLERANCE_C
    # Explicit grade (0); `rejected` above is 1, unmarked frames are neither
    variables["accepted"] = np.array([getattr(c, "rejected") == 0 for c in candidates], dtype=bool)
    # Clouds / transients judged against the frame's own session
    if _SEQ_VARS.search(expression.lower()):
        variables.update(_sequence_vars(candidates, dt, variables))

    # Prefer the plate-solved center/scale over mount-reported values when available
    wcs_ra = col_array("wcs_ra")
    wcs_dec = col_array("wcs_dec")
    ra_arr = np.where(np.isfinite(wcs_ra), wcs_ra, variables["ra"])
    dec_arr = np.where(np.isfinite(wcs_dec), wcs_dec, variables["dec"])
    variables["ra"] = ra_arr
    variables["dec"] = dec_arr
    # Effective pixel scale: solved scale wins, else header-derived
    eff_scale = np.where(np.isfinite(variables["wcs_scale"]), variables["wcs_scale"], variables["pixel_scale"])
    variables["pixel_scale"] = eff_scale
    variables["psf_fwhm_arcsec"] = variables["psf_fwhm"] * eff_scale

    # Field of view (deg): use solved fov_w/fov_h when present, else naxis*scale/3600
    stored_fov_w = col_array("fov_w")
    stored_fov_h = col_array("fov_h")
    derived_fov_w = variables["width"] * eff_scale / 3600.0
    derived_fov_h = variables["height"] * eff_scale / 3600.0
    fov_w = np.where(np.isfinite(stored_fov_w), stored_fov_w, derived_fov_w)
    fov_h = np.where(np.isfinite(stored_fov_h), stored_fov_h, derived_fov_h)
    variables["fov_w"] = fov_w
    variables["fov_h"] = fov_h
    variables["fov"] = np.fmax(fov_w, fov_h)
    rotation = variables["wcs_rotation"]  # NaN when unsolved

    def _angsep(ra0, dec0):
        # Haversine angular separation (deg) from each frame center to (ra0, dec0)
        r1, d1 = np.radians(ra_arr), np.radians(dec_arr)
        r2, d2 = np.radians(float(ra0)), np.radians(float(dec0))
        a = np.sin((d2 - d1) / 2) ** 2 + np.cos(d1) * np.cos(d2) * np.sin((r2 - r1) / 2) ** 2
        return np.degrees(2 * np.arcsin(np.sqrt(np.clip(a, 0, 1))))

    def _contains(ra0, dec0):
        # Rotation-aware FOV-box test in the tangent plane. Uses solved rotation
        # when available (unsolved frames assume 0 = axis-aligned).
        dra = (((float(ra0) - ra_arr + 180.0) % 360.0) - 180.0) * np.cos(np.radians(dec_arr))
        ddec = float(dec0) - dec_arr
        rot = np.radians(np.where(np.isfinite(rotation), rotation, 0.0))
        x = dra * np.cos(rot) + ddec * np.sin(rot)
        y = -dra * np.sin(rot) + ddec * np.cos(rot)
        return (np.abs(x) <= fov_w / 2.0) & (np.abs(y) <= fov_h / 2.0)

    extra = {"angsep": _angsep, "contains": _contains}
    # Backslashes only appear in path patterns; fold them to "/" to match `path` above
    return expr_engine.evaluate(expression.lower().replace("\\", "/"), variables, n, extra_funcs=extra)


def _facets(approved):
    """Integration time + clickable breakdowns (camera/scope/object/date) over the approved set."""
    from collections import Counter

    def top(counter, n=40):
        return [{"value": k, "count": v} for k, v in counter.most_common(n)]

    integration = sum(float(c.exptime or 0) for c in approved)
    return {
        "frames": len(approved),
        "integration_sec": integration,
        "instrume": top(Counter(c.instrume for c in approved if c.instrume)),
        "telescop": top(Counter(c.telescop for c in approved if c.telescop)),
        "object": top(Counter(c.object for c in approved if c.object)),
        "filter": top(Counter(c.filter for c in approved if c.filter)),
        "date": top(Counter(c.date_obs[:10] for c in approved if c.date_obs), 90),
    }


def _stats_summary(candidates):
    out = {}
    for name, col in _STAT_COLS.items():
        attr = col.key
        arr = np.array([getattr(c, attr) for c in candidates if getattr(c, attr) is not None], dtype=float)
        if arr.size == 0:
            continue
        out[name] = {
            "median": round(float(np.median(arr)), 3),
            "mad": round(float(np.median(np.abs(arr - np.median(arr)))), 3),
            "mean": round(float(np.mean(arr)), 3),
            "std": round(float(np.std(arr)), 3),
            "min": round(float(np.min(arr)), 3),
            "max": round(float(np.max(arr)), 3),
        }
    return out


@app.post("/api/query")
def run_query(body: QueryRequest, db: Session = Depends(get_db)):
    candidates = _candidate_query(db, body).all()
    total = len(candidates)
    try:
        mask = _evaluate_candidates(candidates, body.expression)
    except expr_engine.ExpressionError as e:
        raise HTTPException(status_code=400, detail=f"Expression error: {e}")

    approved_idx = [i for i, ok in enumerate(mask) if ok]
    approved_count = len(approved_idx)
    approved = [candidates[i] for i in approved_idx]

    # Paginate approved items for display
    start = (body.page - 1) * body.per_page
    page_idx = approved_idx[start:start + body.per_page]
    items = []
    for i in page_idx:
        d = file_to_dict(candidates[i])
        d.pop("header_json", None)
        items.append(d)

    return {
        "total": total,
        "approved": approved_count,
        "rejected": total - approved_count,
        "stats": _stats_summary(candidates),
        "facets": _facets(approved),
        "items": items,
        "page": body.page,
        "pages": max(1, math.ceil(approved_count / body.per_page)),
        "expression": body.expression,
    }


def _rewrite_path(p: str, src: Optional[str], dst: Optional[str]) -> str:
    """Translate a container path prefix to the host/PixInsight prefix for export."""
    if src and dst and p.startswith(src):
        rewritten = dst + p[len(src):]
        # If the destination looks like a Windows path, normalize separators
        if "\\" in dst or (len(dst) > 1 and dst[1] == ":"):
            rewritten = rewritten.replace("/", "\\")
        return rewritten
    return p


@app.post("/api/export")
def export_list(body: QueryRequest, db: Session = Depends(get_db)):
    candidates = _candidate_query(db, body).all()
    try:
        mask = _evaluate_candidates(candidates, body.expression)
    except expr_engine.ExpressionError as e:
        raise HTTPException(status_code=400, detail=f"Expression error: {e}")

    src = get_config(db, "export_map_from") or config.EXPORT_MAP_FROM
    dst = get_config(db, "export_map_to") or config.EXPORT_MAP_TO
    # Manually-rejected frames are never exported for stacking
    paths = [_rewrite_path(candidates[i].path, src, dst)
             for i, ok in enumerate(mask) if ok and not candidates[i].rejected]
    text = "\n".join(paths) + ("\n" if paths else "")
    return PlainTextResponse(
        text,
        headers={"Content-Disposition": 'attachment; filename="approved_lights.txt"'},
    )


# ── PixInsight FastIntegration (.xpsm) export ─────────────────────────────────
def _ref_fov(c):
    s = c.wcs_scale or c.pixel_scale
    if c.fov_w:
        return c.fov_w
    if c.naxis1 and s:
        return c.naxis1 * s / 3600.0
    return None


def _ref_scale(c):
    return c.wcs_scale or c.pixel_scale


def pick_reference(approved, mode: str):
    """Pick a 'good' (sharp, many stars) reference frame matching a geometry rule."""
    if not approved:
        return None
    # Prefer above-median-quality frames so the reference is sharp with real stars
    measured = [c for c in approved if c.quality_score is not None]
    if measured:
        qs = sorted(c.quality_score for c in measured)
        med = qs[len(qs) // 2]
        good = [c for c in measured if c.quality_score >= med] or measured
    else:
        good = list(approved)

    keyers = {
        "max_fov": (_ref_fov, True), "min_fov": (_ref_fov, False),
        "max_scale": (_ref_scale, True), "min_scale": (_ref_scale, False),
    }
    keyfn, reverse = keyers.get(mode, (_ref_fov, True))
    keyed = [(keyfn(c), c) for c in good]
    keyed = [(k, c) for k, c in keyed if k is not None]
    if not keyed:
        return max(good, key=lambda c: c.quality_score or 0)  # geometry unknown
    keyed.sort(key=lambda x: x[0], reverse=reverse)
    return keyed[0][1]


def build_xpsm(reference_path: str, target_paths: list) -> str:
    from xml.sax.saxutils import escape
    rows = "\n".join(
        f'         <tr>\n            <td id="enabled" value="true"/>\n'
        f'            <td id="image">{escape(p)}</td>\n         </tr>'
        for p in target_paths
    )
    head = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<xpsm version="1.0" xmlns="http://www.pixinsight.com/xpsm" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xsi:schemaLocation="http://www.pixinsight.com/xpsm http://pixinsight.com/xpsm/xpsm-1.0.xsd">\n'
        '   <instance class="FastIntegration" version="2" id="Process01_instance">\n'
        f'      <parameter id="referenceImage">{escape(reference_path)}</parameter>\n'
        f'      <table id="targets" rows="{len(target_paths)}">\n{rows}\n      </table>\n'
    )
    # FastIntegration defaults (from a known-good instance)
    params = """      <parameter id="inputHints"></parameter>
      <parameter id="outputHints"></parameter>
      <parameter id="mode" value="Fast"/>
      <parameter id="generateDrizzleData" value="false"/>
      <parameter id="generateLogFiles" value="false"/>
      <parameter id="generateWeightsFile" value="false"/>
      <parameter id="generateImages" value="false"/>
      <parameter id="generateRejectionMaps" value="true"/>
      <parameter id="noGUIMessages" value="true"/>
      <parameter id="showImages" value="true"/>
      <parameter id="structureLayers" value="5"/>
      <parameter id="noiseLayers" value="0"/>
      <parameter id="hotPixelFilterRadius" value="1"/>
      <parameter id="noiseReductionFilterRadius" value="0"/>
      <parameter id="minStructureSize" value="0"/>
      <parameter id="sensitivity" value="0.50"/>
      <parameter id="peakResponse" value="0.50"/>
      <parameter id="brightThreshold" value="3.00"/>
      <parameter id="localMaximaDetectionLimit" value="0.75"/>
      <parameter id="upperLimit" value="1.000"/>
      <parameter id="matcherTolerance" value="0.0500"/>
      <parameter id="fullAlignmentEnabled" value="false"/>
      <parameter id="ransacTolerance" value="2.00"/>
      <parameter id="ransacMaxIterations" value="2000"/>
      <parameter id="ransacMaximizeInliers" value="1.00"/>
      <parameter id="ransacMaximizeOverlapping" value="1.00"/>
      <parameter id="ransacMaximizeRegularity" value="1.00"/>
      <parameter id="ransacMinimizeError" value="1.00"/>
      <parameter id="maxStars" value="100"/>
      <parameter id="psfTolerance" value="0.50"/>
      <parameter id="useTriangles" value="false"/>
      <parameter id="polygonSides" value="5"/>
      <parameter id="descriptorsPerStar" value="20"/>
      <parameter id="useBrightnessRelations" value="false"/>
      <parameter id="useScaleDifferences" value="false"/>
      <parameter id="scaleTolerance" value="0.100"/>
      <parameter id="preciseAlignmentEnabled" value="false"/>
      <parameter id="preciseAlignmentX0" value="0.00"/>
      <parameter id="preciseAlignmentY0" value="0.00"/>
      <parameter id="minimumSearchSize" value="40"/>
      <parameter id="targetReferenceStarsCount" value="60"/>
      <parameter id="medianErrorTolerance" value="1.5"/>
      <parameter id="maxStarSearchIterations" value="1"/>
      <parameter id="starSearchIterationExpansion" value="20"/>
      <parameter id="rejectionFluxRatio" value="0.1"/>
      <parameter id="useROI" value="false"/>
      <parameter id="roiX0" value="0"/>
      <parameter id="roiY0" value="0"/>
      <parameter id="roiX1" value="0"/>
      <parameter id="roiY1" value="0"/>
      <parameter id="pixelInterpolation" value="BicubicSpline"/>
      <parameter id="clampingThreshold" value="0.30"/>
      <parameter id="weightingEnabled" value="true"/>
      <parameter id="weightingAlgorithm" value="PSFSNR"/>
      <parameter id="rejectionEnabled" value="true"/>
      <parameter id="rejectionAlgorithm" value="WinsorizedSigmaClipping"/>
      <parameter id="sigmaLow" value="3.0"/>
      <parameter id="sigmaHigh" value="3.0"/>
      <parameter id="parallelReadings" value="0"/>
      <parameter id="parallelWritings" value="0"/>
      <parameter id="processorsUsed" value="0"/>
      <parameter id="integrationBatchSize" value="50"/>
      <parameter id="integrationPrefetchSize" value="50"/>
      <parameter id="outputDirectory"></parameter>
      <parameter id="outputExtension">.xisf</parameter>
      <parameter id="outputPrefix"></parameter>
      <parameter id="outputPostfix">_r</parameter>
      <parameter id="overwriteExistingFiles" value="false"/>
      <table id="outputData" rows="0"/>
"""
    tail = ('   </instance>\n'
            '   <icon id="Process01" instance="Process01_instance" xpos="100" ypos="100" workspace="Workspace01"/>\n'
            '</xpsm>\n')
    return head + params + tail


# ── Routes: Saved queries ─────────────────────────────────────────────────────
class SavedQueryBody(BaseModel):
    name: str
    folder: str = ""
    query: dict = {}


def _saved_to_dict(r):
    return {"id": r.id, "name": r.name, "folder": r.folder or "",
            "query": json.loads(r.query_json or "{}"),
            "updated_at": r.updated_at.isoformat() if r.updated_at else None}


@app.get("/api/queries")
def list_queries(db: Session = Depends(get_db)):
    rows = db.query(SavedQuery).order_by(SavedQuery.folder, SavedQuery.name).all()
    return [_saved_to_dict(r) for r in rows]


@app.post("/api/queries", status_code=201)
def create_query(body: SavedQueryBody, db: Session = Depends(get_db)):
    if not body.name.strip():
        raise HTTPException(status_code=400, detail="Name required")
    q = SavedQuery(name=body.name.strip(), folder=body.folder.strip().strip("/"),
                   query_json=json.dumps(body.query),
                   created_at=datetime.utcnow(), updated_at=datetime.utcnow())
    db.add(q); db.commit(); db.refresh(q)
    return _saved_to_dict(q)


@app.put("/api/queries/{qid}")
def update_query(qid: int, body: SavedQueryBody, db: Session = Depends(get_db)):
    q = db.query(SavedQuery).filter(SavedQuery.id == qid).first()
    if not q:
        raise HTTPException(status_code=404, detail="Not found")
    q.name = body.name.strip()
    q.folder = body.folder.strip().strip("/")
    q.query_json = json.dumps(body.query)
    q.updated_at = datetime.utcnow()
    db.commit()
    return _saved_to_dict(q)


@app.delete("/api/queries/{qid}", status_code=204)
def delete_query(qid: int, db: Session = Depends(get_db)):
    db.query(SavedQuery).filter(SavedQuery.id == qid).delete()
    db.commit()


@app.post("/api/platesolve/selection")
def platesolve_selection(body: QueryRequest, db: Session = Depends(get_db)):
    """Enqueue ASTAP solves for every frame matching the current query."""
    _require_queue()
    if not _astap_status(db)["available"]:
        raise HTTPException(status_code=400, detail="ASTAP not ready — set the binary path and a star database first.")
    candidates = _candidate_query(db, body).all()
    try:
        mask = _evaluate_candidates(candidates, body.expression)
    except expr_engine.ExpressionError as e:
        raise HTTPException(status_code=400, detail=f"Expression error: {e}")
    ids = [candidates[i].id for i, ok in enumerate(mask) if ok]
    jobqueue.enqueue([{"op": "solve", "id": i} for i in ids])
    return {"queued": len(ids)}


@app.post("/api/export/xpsm")
def export_xpsm(body: QueryRequest, ref: str = Query("max_fov"), db: Session = Depends(get_db)):
    candidates = _candidate_query(db, body).all()
    try:
        mask = _evaluate_candidates(candidates, body.expression)
    except expr_engine.ExpressionError as e:
        raise HTTPException(status_code=400, detail=f"Expression error: {e}")

    approved = [candidates[i] for i, ok in enumerate(mask) if ok and not candidates[i].rejected]
    if not approved:
        raise HTTPException(status_code=400, detail="No approved frames to export.")

    reference = pick_reference(approved, ref)
    src = get_config(db, "export_map_from") or config.EXPORT_MAP_FROM
    dst = get_config(db, "export_map_to") or config.EXPORT_MAP_TO
    targets = [_rewrite_path(c.path, src, dst) for c in approved]
    ref_path = _rewrite_path(reference.path, src, dst)

    xml = build_xpsm(ref_path, targets)
    return PlainTextResponse(
        xml, media_type="application/xml",
        headers={"Content-Disposition": 'attachment; filename="FastIntegration.xpsm"'},
    )


# ── User-defined targets (spatial matching) ───────────────────────────────────
def frames_for_target(db, ra0, dec0, radius_deg=None):
    """
    Light frames whose field of view covers (ra0, dec0) — rotation-aware, using the
    plate-solved centre when available. With radius_deg, also include frames whose
    centre lies within that radius (target larger than / overlapping the frame).
    """
    rows = (db.query(FitsFile)
            .filter(FitsFile.imagetyp == "Light Frame")
            .filter(FitsFile.index_error.is_(None))
            .all())
    rows = [r for r in rows
            if (r.wcs_ra is not None or r.ra is not None)
            and (r.wcs_dec is not None or r.dec is not None)]
    if not rows:
        return []

    ra = np.array([r.wcs_ra if r.wcs_ra is not None else r.ra for r in rows], dtype=float)
    dec = np.array([r.wcs_dec if r.wcs_dec is not None else r.dec for r in rows], dtype=float)
    scale = np.array([r.wcs_scale if r.wcs_scale is not None
                      else (r.pixel_scale if r.pixel_scale is not None else np.nan)
                      for r in rows], dtype=float)
    nx = np.array([r.naxis1 if r.naxis1 else np.nan for r in rows], dtype=float)
    ny = np.array([r.naxis2 if r.naxis2 else np.nan for r in rows], dtype=float)
    fw = np.array([r.fov_w if r.fov_w else np.nan for r in rows], dtype=float)
    fh = np.array([r.fov_h if r.fov_h else np.nan for r in rows], dtype=float)
    fov_w = np.where(np.isfinite(fw), fw, nx * scale / 3600.0)
    fov_h = np.where(np.isfinite(fh), fh, ny * scale / 3600.0)
    rot = np.array([r.wcs_rotation if r.wcs_rotation is not None else np.nan for r in rows], dtype=float)

    # Rotation-aware FOV-box test in the tangent plane
    dra = (((float(ra0) - ra + 180.0) % 360.0) - 180.0) * np.cos(np.radians(dec))
    ddec = float(dec0) - dec
    rr = np.radians(np.where(np.isfinite(rot), rot, 0.0))
    x = dra * np.cos(rr) + ddec * np.sin(rr)
    y = -dra * np.sin(rr) + ddec * np.cos(rr)
    with np.errstate(invalid="ignore"):
        inside = (np.abs(x) <= fov_w / 2.0) & (np.abs(y) <= fov_h / 2.0)
        if radius_deg:
            sep = np.degrees(np.arccos(np.clip(
                np.sin(np.radians(dec)) * np.sin(np.radians(float(dec0))) +
                np.cos(np.radians(dec)) * np.cos(np.radians(float(dec0))) *
                np.cos(np.radians(ra - float(ra0))), -1, 1)))
            inside = inside | (sep <= float(radius_deg))
    return [r for r, ok in zip(rows, inside) if bool(ok)]


def _rollup(frames):
    """Shared rollup shape used by both header-object and user-target views."""
    from collections import Counter
    nights = sorted({(f.date_obs or "")[:10] for f in frames if f.date_obs})
    by_filter = {}
    for f in frames:
        e = by_filter.setdefault(f.filter or "—", {"filter": f.filter or "—", "frames": 0, "integration_sec": 0.0})
        e["frames"] += 1
        e["integration_sec"] += float(f.exptime or 0)
    rigs = Counter((f.telescop, f.instrume) for f in frames if f.telescop or f.instrume)
    q = [f.quality_score for f in frames if f.quality_score is not None]
    hfr = [f.median_hfr for f in frames if f.median_hfr is not None]
    return {
        "frames": len(frames),
        "integration_sec": sum(float(f.exptime or 0) for f in frames),
        "nights": len(nights),
        "first_night": nights[0] if nights else None,
        "last_night": nights[-1] if nights else None,
        "avg_quality": round(sum(q) / len(q), 1) if q else None,
        "avg_hfr": round(sum(hfr) / len(hfr), 2) if hfr else None,
        "rejected": sum(1 for f in frames if f.rejected),
        "filters": sorted(by_filter.values(), key=lambda x: -x["integration_sec"]),
        "rigs": [{"telescop": t, "instrume": c, "frames": n} for (t, c), n in rigs.most_common()],
    }


class TargetBody(BaseModel):
    name: str
    ra: float
    dec: float
    radius_deg: Optional[float] = None
    notes: Optional[str] = None


def _target_dict(t):
    return {"id": t.id, "name": t.name, "ra": t.ra, "dec": t.dec,
            "radius_deg": t.radius_deg, "notes": t.notes}


@app.get("/api/user-targets")
def list_user_targets(rollup: bool = Query(True), db: Session = Depends(get_db)):
    """User-defined targets, each with a spatially-matched frame rollup."""
    out = []
    for t in db.query(Target).order_by(Target.name).all():
        d = _target_dict(t)
        if rollup:
            d.update(_rollup(frames_for_target(db, t.ra, t.dec, t.radius_deg)))
        out.append(d)
    if rollup:
        out.sort(key=lambda x: -x.get("integration_sec", 0))
    return {"targets": out,
            "total_integration_sec": sum(x.get("integration_sec", 0) for x in out),
            "target_count": len(out)}


@app.post("/api/user-targets", status_code=201)
def create_user_target(body: TargetBody, db: Session = Depends(get_db)):
    if not body.name.strip():
        raise HTTPException(status_code=400, detail="Name required")
    t = Target(name=body.name.strip(), ra=body.ra, dec=body.dec,
               radius_deg=body.radius_deg, notes=body.notes, created_at=datetime.utcnow())
    db.add(t); db.commit(); db.refresh(t)
    return _target_dict(t)


@app.post("/api/user-targets/from-object", status_code=201)
def create_target_from_object(object: str = Query(...), db: Session = Depends(get_db)):
    """Create a custom target at the median solved position of an OBJECT's frames."""
    rows = (db.query(FitsFile.wcs_ra, FitsFile.wcs_dec, FitsFile.ra, FitsFile.dec)
            .filter(FitsFile.object == object)
            .filter(FitsFile.imagetyp == "Light Frame")
            .filter(FitsFile.index_error.is_(None)).all())
    ras = [(r.wcs_ra if r.wcs_ra is not None else r.ra) for r in rows]
    decs = [(r.wcs_dec if r.wcs_dec is not None else r.dec) for r in rows]
    ras = [v for v in ras if v is not None]
    decs = [v for v in decs if v is not None]
    if not ras or not decs:
        raise HTTPException(status_code=400,
                            detail=f"No frames with coordinates for '{object}' — plate-solve them first.")
    t = Target(name=object, ra=float(np.median(ras)), dec=float(np.median(decs)),
               created_at=datetime.utcnow())
    db.add(t); db.commit(); db.refresh(t)
    return _target_dict(t)


@app.put("/api/user-targets/{tid}")
def update_user_target(tid: int, body: TargetBody, db: Session = Depends(get_db)):
    t = db.query(Target).filter(Target.id == tid).first()
    if not t:
        raise HTTPException(status_code=404, detail="Not found")
    t.name, t.ra, t.dec = body.name.strip(), body.ra, body.dec
    t.radius_deg, t.notes = body.radius_deg, body.notes
    db.commit()
    return _target_dict(t)


@app.delete("/api/user-targets/{tid}", status_code=204)
def delete_user_target(tid: int, db: Session = Depends(get_db)):
    db.query(Target).filter(Target.id == tid).delete()
    db.commit()


@app.get("/api/user-targets/{tid}/detail")
def user_target_detail(tid: int, db: Session = Depends(get_db)):
    """Per-night breakdown of the frames covering this target."""
    t = db.query(Target).filter(Target.id == tid).first()
    if not t:
        raise HTTPException(status_code=404, detail="Not found")
    frames = frames_for_target(db, t.ra, t.dec, t.radius_deg)

    nights = {}
    for f in frames:
        n = (f.date_obs or "Unknown")[:10]
        e = nights.setdefault(n, {"night": n, "frames": 0, "integration_sec": 0.0,
                                  "telescop": f.telescop, "instrume": f.instrume,
                                  "moon_illum": f.moon_illum, "_by_filter": {}})
        e["frames"] += 1
        e["integration_sec"] += float(f.exptime or 0)
        fe = e["_by_filter"].setdefault(f.filter or "—",
                                        {"filter": f.filter or "—", "frames": 0,
                                         "integration_sec": 0.0, "_hfr": [], "_ecc": [],
                                         "_q": [], "_alt": []})
        fe["frames"] += 1
        fe["integration_sec"] += float(f.exptime or 0)
        for key, val in (("_hfr", f.median_hfr), ("_ecc", f.eccentricity),
                         ("_q", f.quality_score), ("_alt", f.altitude)):
            if val is not None:
                fe[key].append(val)

    def avg(v, nd):
        return round(sum(v) / len(v), nd) if v else None

    night_list = []
    for e in nights.values():
        e["filters"] = []
        for fe in e.pop("_by_filter").values():
            e["filters"].append({
                "filter": fe["filter"], "frames": fe["frames"],
                "integration_sec": fe["integration_sec"],
                "avg_hfr": avg(fe["_hfr"], 2), "avg_eccentricity": avg(fe["_ecc"], 3),
                "avg_quality": avg(fe["_q"], 1), "avg_altitude": avg(fe["_alt"], 1),
            })
        night_list.append(e)
    night_list.sort(key=lambda x: x["night"], reverse=True)

    return {**_target_dict(t), **_rollup(frames), "nights": night_list,
            "night_count": len(night_list)}


# ── Routes: Targets (per-object dashboard) ────────────────────────────────────
def _lights(db):
    """Base query: real (non-tombstone) light frames."""
    return (db.query(FitsFile)
            .filter(FitsFile.imagetyp == "Light Frame")
            .filter(FitsFile.index_error.is_(None)))


@app.get("/api/targets")
def list_targets(db: Session = Depends(get_db)):
    """One row per object: integration, frames, nights, filters, rigs, quality."""
    night = func.substr(FitsFile.date_obs, 1, 10)
    rows = (
        db.query(
            FitsFile.object,
            func.count(FitsFile.id),
            func.sum(FitsFile.exptime),
            func.count(distinct(night)),
            func.min(night), func.max(night),
            func.avg(FitsFile.quality_score),
            func.avg(FitsFile.median_hfr),
            func.sum(func.coalesce(FitsFile.rejected, 0)),
        )
        .filter(FitsFile.imagetyp == "Light Frame")
        .filter(FitsFile.index_error.is_(None))
        .filter(FitsFile.object.isnot(None))
        .group_by(FitsFile.object)
        .all()
    )

    # Per-object filter + rig breakdowns in two more grouped queries
    filt_rows = (
        db.query(FitsFile.object, FitsFile.filter,
                 func.count(FitsFile.id), func.sum(FitsFile.exptime))
        .filter(FitsFile.imagetyp == "Light Frame")
        .filter(FitsFile.index_error.is_(None))
        .group_by(FitsFile.object, FitsFile.filter).all()
    )
    by_filter = {}
    for obj, filt, cnt, secs in filt_rows:
        by_filter.setdefault(obj, []).append(
            {"filter": filt or "—", "frames": int(cnt), "integration_sec": float(secs or 0)})

    rig_rows = (
        db.query(FitsFile.object, FitsFile.telescop, FitsFile.instrume, func.count(FitsFile.id))
        .filter(FitsFile.imagetyp == "Light Frame")
        .filter(FitsFile.index_error.is_(None))
        .group_by(FitsFile.object, FitsFile.telescop, FitsFile.instrume).all()
    )
    by_rig = {}
    for obj, tel, cam, cnt in rig_rows:
        if tel or cam:
            by_rig.setdefault(obj, []).append(
                {"telescop": tel, "instrume": cam, "frames": int(cnt)})

    targets = []
    for obj, frames, secs, nights, first, last, avg_q, avg_hfr, rejected in rows:
        targets.append({
            "object": obj,
            "frames": int(frames),
            "integration_sec": float(secs or 0),
            "nights": int(nights or 0),
            "first_night": first, "last_night": last,
            "avg_quality": round(avg_q, 1) if avg_q is not None else None,
            "avg_hfr": round(avg_hfr, 2) if avg_hfr is not None else None,
            "rejected": int(rejected or 0),
            "filters": sorted(by_filter.get(obj, []), key=lambda f: -f["integration_sec"]),
            "rigs": sorted(by_rig.get(obj, []), key=lambda r: -r["frames"]),
        })
    targets.sort(key=lambda t: -t["integration_sec"])
    return {
        "targets": targets,
        "total_integration_sec": sum(t["integration_sec"] for t in targets),
        "target_count": len(targets),
    }


@app.get("/api/targets/{name}")
def target_detail(name: str, db: Session = Depends(get_db)):
    """Per-night breakdown for one object, plus per-filter totals and quality."""
    night = func.substr(FitsFile.date_obs, 1, 10)
    rows = (
        db.query(
            night.label("night"), FitsFile.filter,
            func.count(FitsFile.id), func.sum(FitsFile.exptime),
            func.avg(FitsFile.median_hfr), func.avg(FitsFile.eccentricity),
            func.avg(FitsFile.quality_score), func.avg(FitsFile.moon_illum),
            func.avg(FitsFile.altitude),
            FitsFile.telescop, FitsFile.instrume,
        )
        .filter(FitsFile.imagetyp == "Light Frame")
        .filter(FitsFile.index_error.is_(None))
        .filter(FitsFile.object == name)
        .group_by(night, FitsFile.filter)
        .order_by(night.desc())
        .all()
    )

    nights, totals = {}, {}
    for (n, filt, cnt, secs, hfr, ecc, q, moon, alt, tel, cam) in rows:
        secs = float(secs or 0)
        entry = nights.setdefault(n or "Unknown", {
            "night": n or "Unknown", "frames": 0, "integration_sec": 0.0,
            "telescop": tel, "instrume": cam, "filters": [],
            "moon_illum": round(moon, 2) if moon is not None else None,
        })
        entry["frames"] += int(cnt)
        entry["integration_sec"] += secs
        entry["filters"].append({
            "filter": filt or "—", "frames": int(cnt), "integration_sec": secs,
            "avg_hfr": round(hfr, 2) if hfr is not None else None,
            "avg_eccentricity": round(ecc, 3) if ecc is not None else None,
            "avg_quality": round(q, 1) if q is not None else None,
            "avg_altitude": round(alt, 1) if alt is not None else None,
        })
        t = totals.setdefault(filt or "—", {"filter": filt or "—", "frames": 0, "integration_sec": 0.0})
        t["frames"] += int(cnt)
        t["integration_sec"] += secs

    night_list = sorted(nights.values(), key=lambda x: x["night"], reverse=True)
    return {
        "object": name,
        "nights": night_list,
        "night_count": len(night_list),
        "filters": sorted(totals.values(), key=lambda f: -f["integration_sec"]),
        "frames": sum(n["frames"] for n in night_list),
        "integration_sec": sum(n["integration_sec"] for n in night_list),
    }


# ── Routes: Sessions ──────────────────────────────────────────────────────────
@app.get("/api/sessions")
def list_sessions(db: Session = Depends(get_db)):
    """
    Group light frames into imaging sessions (target + night), with per-filter
    integration time. Night bucket = local-ish calendar date of the observation.
    """
    night = func.substr(FitsFile.date_obs, 1, 10)
    rows = (
        db.query(
            FitsFile.object,
            FitsFile.filter,
            night.label("night"),
            func.count(FitsFile.id),
            func.sum(FitsFile.exptime),
            func.avg(FitsFile.median_hfr),
            func.avg(FitsFile.eccentricity),
            func.avg(FitsFile.quality_score),
            FitsFile.telescop,
            FitsFile.instrume,
        )
        .filter(FitsFile.imagetyp == "Light Frame")
        .filter(FitsFile.date_obs.isnot(None))
        .group_by(FitsFile.object, FitsFile.filter, night)
        .order_by(night.desc())
        .all()
    )

    # Nest per (object, night), with a filter breakdown inside.
    sessions = {}
    for obj, filt, night_str, count, exptime_sum, avg_hfr, avg_ecc, avg_q, telescop, instrume in rows:
        key = (obj or "Unknown", night_str or "Unknown")
        s = sessions.setdefault(key, {
            "object": obj or "Unknown",
            "night": night_str or "Unknown",
            "telescop": telescop,
            "instrume": instrume,
            "frames": 0,
            "integration_sec": 0.0,
            "filters": [],
        })
        secs = float(exptime_sum or 0)
        s["frames"] += int(count)
        s["integration_sec"] += secs
        s["filters"].append({
            "filter": filt or "—",
            "frames": int(count),
            "integration_sec": secs,
            "avg_hfr": round(avg_hfr, 2) if avg_hfr is not None else None,
            "avg_eccentricity": round(avg_ecc, 3) if avg_ecc is not None else None,
            "avg_quality": round(avg_q, 1) if avg_q is not None else None,
        })

    result = sorted(sessions.values(), key=lambda x: x["night"], reverse=True)
    total_integration = sum(s["integration_sec"] for s in result)
    return {"sessions": result, "total_integration_sec": total_integration, "session_count": len(result)}


# ── Routes: Calibration coverage ──────────────────────────────────────────────
@app.get("/api/calibration")
def calibration_coverage(db: Session = Depends(get_db)):
    """Light setups vs the darks / flats / bias in the catalog (see calibration.py)."""
    import calibration
    rows = (db.query(FitsFile.imagetyp, FitsFile.instrume, FitsFile.filter, FitsFile.xbinning,
                     FitsFile.gain, FitsFile.offset, FitsFile.exptime, FitsFile.ccd_temp,
                     FitsFile.date_obs, FitsFile.object)
            .filter(FitsFile.index_error.is_(None))
            .filter(FitsFile.imagetyp.in_(calibration.FRAME_TYPES))
            .all())
    return calibration.report(rows, config.CAL_TEMP_TOLERANCE, config.CAL_FLAT_MAX_DAYS)


# ── Routes: Prune ─────────────────────────────────────────────────────────────
@app.post("/api/prune")
def prune_missing(db: Session = Depends(get_db)):
    """Remove DB rows (and thumbnails) for files no longer on disk."""
    removed = 0
    for f in db.query(FitsFile.id, FitsFile.path, FitsFile.thumbnail).all():
        if not os.path.exists(f.path):
            if f.thumbnail:
                (THUMBNAILS_DIR / f.thumbnail).unlink(missing_ok=True)
            for preview in config.PREVIEWS_DIR.glob(f"{f.id}-*.jpg"):
                preview.unlink(missing_ok=True)
            db.query(FitsFile).filter(FitsFile.id == f.id).delete()
            removed += 1
    db.commit()
    return {"removed": removed}


# ── Routes: Reset ─────────────────────────────────────────────────────────────
@app.post("/api/reset")
def reset_database():
    import shutil
    from database import Base, engine
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    # clear thumbnails
    for f in THUMBNAILS_DIR.iterdir():
        if f.name != '.gitkeep':
            f.unlink(missing_ok=True)
    for f in config.PREVIEWS_DIR.glob("*.jpg"):
        f.unlink(missing_ok=True)
    return {"ok": True}


# ── Routes: Stats ─────────────────────────────────────────────────────────────
@app.get("/api/stats")
def get_stats(db: Session = Depends(get_db)):
    total = db.query(func.count(FitsFile.id)).filter(FitsFile.index_error.is_(None)).scalar()
    failed = db.query(func.count(FitsFile.id)).filter(FitsFile.index_error.isnot(None)).scalar()

    by_imagetyp = (
        db.query(FitsFile.imagetyp, func.count(FitsFile.id))
        .group_by(FitsFile.imagetyp)
        .all()
    )
    by_object = (
        db.query(FitsFile.object, func.count(FitsFile.id))
        .filter(FitsFile.object.isnot(None))
        .group_by(FitsFile.object)
        .order_by(func.count(FitsFile.id).desc())
        .limit(20)
        .all()
    )
    by_filter = (
        db.query(FitsFile.filter, func.count(FitsFile.id))
        .filter(FitsFile.filter.isnot(None))
        .group_by(FitsFile.filter)
        .all()
    )

    # Distinct values for filter dropdowns
    imagetypes = [r[0] for r in db.query(distinct(FitsFile.imagetyp)).filter(FitsFile.imagetyp.isnot(None)).all()]
    filters = [r[0] for r in db.query(distinct(FitsFile.filter)).filter(FitsFile.filter.isnot(None)).all()]
    telescopes = [r[0] for r in db.query(distinct(FitsFile.telescop)).filter(FitsFile.telescop.isnot(None)).all()]
    instruments = [r[0] for r in db.query(distinct(FitsFile.instrume)).filter(FitsFile.instrume.isnot(None)).all()]

    return {
        "total": total,
        "failed": failed,
        "by_imagetyp": {k or "Unknown": v for k, v in by_imagetyp},
        "by_object": {k: v for k, v in by_object},
        "by_filter": {k: v for k, v in by_filter},
        "distinct": {
            "imagetypes": sorted(imagetypes),
            "filters": sorted(filters),
            "telescopes": sorted(telescopes),
            "instruments": sorted(instruments),
        },
    }


@app.get("/api/health")
def health():
    return {"ok": True, "data_dir": str(config.DATA_DIR)}


# ── Public read-only API (/api/v1, own docs at /api/v1/docs) ──────────────────
from public_api import public_app  # noqa: E402
app.mount("/api/v1", public_app)


# ── Frontend (production/container) ───────────────────────────────────────────
# Mounted LAST so /api and /thumbnails take precedence. Absent in dev, where
# Vite serves the frontend on its own port.
if config.STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(config.STATIC_DIR), html=True), name="frontend")
