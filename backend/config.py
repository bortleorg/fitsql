"""
Central runtime configuration, driven by environment variables so the app can
run self-contained in a container with only volume mounts + env set.

Persistent state (SQLite DB, thumbnails, ASTAP star database) lives under
DATA_DIR — mount that to a volume. The media to index is mounted separately and
referenced by the watched paths.
"""
import os
from pathlib import Path


def _env_path(name, default):
    return Path(os.environ.get(name) or default).expanduser()


# Where persistent app state lives (DB, thumbnails, star DB). Defaults to the
# backend dir for local dev; set DATA_DIR=/config in the container.
DATA_DIR = _env_path("DATA_DIR", Path(__file__).resolve().parent)
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Pre-rename installs keep their existing catalog.db rather than starting empty
_legacy_db = DATA_DIR / "catalog.db"
DB_PATH = _env_path("DB_PATH", _legacy_db if _legacy_db.exists() and not (DATA_DIR / "fitsql.db").exists()
                    else DATA_DIR / "fitsql.db")
DATABASE_URL = f"sqlite:///{DB_PATH.as_posix()}"

THUMBNAILS_DIR = _env_path("THUMBNAILS_DIR", DATA_DIR / "thumbnails")
THUMBNAILS_DIR.mkdir(parents=True, exist_ok=True)

# Larger on-demand previews for zoom/compare (JPEG cache, safe to delete)
PREVIEWS_DIR = _env_path("PREVIEWS_DIR", DATA_DIR / "previews")
PREVIEW_MAX_PX = int(os.environ.get("PREVIEW_MAX_PX", "2048"))
# 100% viewer (pixel peeping): full-resolution stretched frames kept in memory so
# panning only slices tiles — about 60 MB per 61 MP mono frame (3× for colour)
PEEP_CACHE_MB = int(os.environ.get("PEEP_CACHE_MB", "512"))

# Built frontend (FastAPI serves it). Set in the container; absent in dev.
STATIC_DIR = _env_path("STATIC_DIR", Path(__file__).resolve().parent / "static")

# ASTAP plate solver. ASTAP_PATH may also be set via the UI (stored in DB);
# this env value is the default seeded on first boot.
ASTAP_PATH = os.environ.get("ASTAP_PATH") or None
ASTAP_DB_DIR = _env_path("ASTAP_DB_DIR", DATA_DIR / "astap_db")

# Optional: auto-seed watched paths on first boot (comma-separated container paths)
WATCH_PATHS = [p.strip() for p in os.environ.get("WATCH_PATHS", "").split(",") if p.strip()]

# Directory-picker roots — the folder browser is confined to these (no escaping
# above them). Defaults to /mnt, where the media volume is typically mounted; the
# container only sees what you actually mount there.
BROWSE_ROOTS = [p.strip() for p in os.environ.get("BROWSE_ROOTS", "/mnt").split(",") if p.strip()]

# Optional default observing site (used when a frame's header lacks coordinates)
SITE_LAT = os.environ.get("SITE_LAT")
SITE_LON = os.environ.get("SITE_LON")
SITE_ELEV = os.environ.get("SITE_ELEV")

# Optional export path rewrite: container path prefix -> host/PixInsight prefix
EXPORT_MAP_FROM = os.environ.get("EXPORT_MAP_FROM") or None
EXPORT_MAP_TO = os.environ.get("EXPORT_MAP_TO") or None

# Public read-only API (/api/v1): comma-separated keys. When set, every data
# endpoint requires one (X-API-Key header or api_key query param); unset = open.
PUBLIC_API_KEYS = [k.strip() for k in os.environ.get("PUBLIC_API_KEYS", "").split(",") if k.strip()]

# ── Queue / workers ──────────────────────────────────────────────────────────
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
# How many file jobs a single worker process handles concurrently (threads).
# Horizontal scaling is done by running more worker *containers*.
WORKER_CONCURRENCY = int(os.environ.get("WORKER_CONCURRENCY", "2"))

# ── Star detection (SEP) ─────────────────────────────────────────────────────
# Star detection tuned for speed on deep/crowded fields. At 3σ these fields
# detect tens of thousands of noise/nebula peaks and SEP's deblender dominates
# runtime (and OOMs). Benchmarks showed 8σ + minarea 9 + gentle deblend cuts
# sep.extract 5-8x (e.g. 54s -> 9s on a 900s H frame) with <6% FWHM shift, since
# the metrics use the brightest ~200 stars either way. All env-tunable.
SEP_DETECT_THRESH = float(os.environ.get("SEP_DETECT_THRESH", "8.0"))
SEP_DEBLEND_NTHRESH = int(os.environ.get("SEP_DEBLEND_NTHRESH", "8"))
SEP_DEBLEND_CONT = float(os.environ.get("SEP_DEBLEND_CONT", "0.1"))
SEP_MINAREA = int(os.environ.get("SEP_MINAREA", "9"))
SEP_PIXSTACK = int(os.environ.get("SEP_PIXSTACK", "1000000"))
# sep.extract's run time grows much faster than the source count within one call,
# and deblending isn't the cause (deblend off / nthresh 1 were no faster): a 102 MP
# drizzled master (43 600 sources) took 367 s in one pass, a crowded 61 MP sub 44 s.
# Large frames are therefore extracted in SEP_TILE_PX tiles — at 2048/1024/512/256
# px the master took 35/14/8.5/21 s (small tiles pay SEP's per-call setup) and the
# sub 4.5/1.9/3.9 s, with the same detections ±2. Threads over tiles didn't help.
# Each tile is grown by SEP_TILE_OVERLAP px so stars on a
# tile border are seen whole; a source belongs to the tile its centroid falls in.
# Only sources larger than the overlap (big saturated halos, nebula knots) can be
# cut at a tile border — they come out flagged truncated, as at the frame edge.
# Frames no larger than one tile run a single pass, exactly as before. 0 = never tile.
SEP_TILE_PX = int(os.environ.get("SEP_TILE_PX", "1024"))
SEP_TILE_OVERLAP = int(os.environ.get("SEP_TILE_OVERLAP", "64"))

# ── Trailing / tracking detection ───────────────────────────────────────────
# trail_score = fraction of the brightest stars that are elongated (a/b above
# TRAIL_ELONGATION) AND aligned within TRAIL_ALIGN_DEG of the frame's dominant
# direction. Mount slips / bad tracking smear every star the same way; tilt and
# coma elongate stars in directions that vary across the field. Frames scoring
# above TRAIL_THRESHOLD are `trailed` in selector expressions. Calibrated on 120
# catalog frames reviewed by eye: clean frames scored <=0.023, trailed >=0.039.
# Exposures shorter than TRAIL_MIN_EXPOSURE_S (lunar/planetary) can't trail and
# score 0 — their detections are surface detail, not stars.
TRAIL_ELONGATION = float(os.environ.get("TRAIL_ELONGATION", "2.0"))
TRAIL_ALIGN_DEG = float(os.environ.get("TRAIL_ALIGN_DEG", "15"))
TRAIL_THRESHOLD = float(os.environ.get("TRAIL_THRESHOLD", "0.03"))
TRAIL_MIN_EXPOSURE_S = float(os.environ.get("TRAIL_MIN_EXPOSURE_S", "1.0"))
# SEP deblends a satellite / aircraft streak into a chain of long thin fragments that
# all point along it, which reads as coherent trailing. Chains of at least
# TRAIL_LINE_MIN elongated sources are left out of trail_score: each link points the
# same way (within TRAIL_ALIGN_DEG), sits on the line through its neighbour (offset
# under their width plus TRAIL_ALIGN_DEG of the gap, never over TRAIL_LINE_BAND_PX)
# and is no further along it than TRAIL_LINE_GAP times their mean length. Trailed
# stars are short and scattered over the field, and a star smeared into parallel
# dashes by a guiding wander lies across its dashes, so neither chains.
TRAIL_LINE_BAND_PX = float(os.environ.get("TRAIL_LINE_BAND_PX", "15"))
TRAIL_LINE_GAP = float(os.environ.get("TRAIL_LINE_GAP", "3"))
TRAIL_LINE_MIN = int(os.environ.get("TRAIL_LINE_MIN", "3"))
# quality_score ceiling for trailed frames — a trailed sub is unusable however sharp
# its deblended star fragments look, so it must not rank with good frames
TRAIL_QUALITY_CAP = float(os.environ.get("TRAIL_QUALITY_CAP", "25"))

# ── Star sample for shape metrics ────────────────────────────────────────────
# FWHM / HFR / eccentricity are measured on the brightest *clean* stars.
# Saturated cores are flat-topped and inflate FWHM/HFR, stars cut by the frame
# edge are truncated, and deblended blends mix two profiles — all dropped first.
# A star is saturated when its peak reaches STAR_SAT_FRACTION of the frame's
# max pixel value. If fewer than STAR_MIN_CLEAN stars survive, blends are let
# back in (crowded fields deblend almost everything).
STAR_SAT_FRACTION = float(os.environ.get("STAR_SAT_FRACTION", "0.95"))
STAR_EDGE_PX = int(os.environ.get("STAR_EDGE_PX", "16"))
STAR_MIN_CLEAN = int(os.environ.get("STAR_MIN_CLEAN", "30"))

# ── Grid screening (local occlusion / patchy cloud / gradients) ─────────────
# Global star count and HFR miss a tree line in one corner. The frame is split
# into GRID_COLS × GRID_ROWS cells: a cell is empty when it holds fewer than
# GRID_EMPTY_FRACTION of the median cell's stars, and `empty_cells` is the share
# of the grid covered by the largest *connected* patch of empty cells (isolated
# empty cells come from noise, vignetting and nebulosity, not occlusions).
# Fields whose median cell has fewer than GRID_MIN_MEDIAN_STARS are too sparse
# to judge (empty_cells stays unknown). Frames above OCCLUDED_THRESHOLD are
# `occluded` in selector expressions.
GRID_COLS = int(os.environ.get("GRID_COLS", "8"))
GRID_ROWS = int(os.environ.get("GRID_ROWS", "6"))
GRID_EMPTY_FRACTION = float(os.environ.get("GRID_EMPTY_FRACTION", "0.2"))
GRID_MIN_MEDIAN_STARS = float(os.environ.get("GRID_MIN_MEDIAN_STARS", "5"))
OCCLUDED_THRESHOLD = float(os.environ.get("OCCLUDED_THRESHOLD", "0.15"))

# ── Background dips (frost / dew on the optics) ─────────────────────────────
# Frost or dew on the sensor window, a filter or a corrector dims sky and stars in a
# patch thousands of pixels across — a smooth shadow, or thousands of tiny dark
# donuts when droplets / ice crystals sit on the cover glass — while star counts,
# HFR and bg_spread barely move. The star-clipped background is mapped in blocks of
# at least DIP_BLOCK_MIN px (about DIP_MAP_PX blocks along the long side), fitted
# with a DIP_FIT_DEGREE polynomial surface (vignetting, gradients) and the residual
# smoothed over DIP_SMOOTH of the frame; `bg_dip` is its deepest point at least
# DIP_EDGE of the frame from the edges, as a fraction of the background (a dip that
# runs off the frame edge is an unfitted gradient — clouds, twilight — and skipped). Frames
# above SHADOW_THRESHOLD are `shadow` in selector expressions. Dark nebulae read as
# dips too — `dip_rise` compares a frame with its own session instead.
DIP_MAP_PX = int(os.environ.get("DIP_MAP_PX", "512"))
DIP_BLOCK_MIN = int(os.environ.get("DIP_BLOCK_MIN", "16"))
DIP_FIT_DEGREE = int(os.environ.get("DIP_FIT_DEGREE", "4"))
DIP_SMOOTH = float(os.environ.get("DIP_SMOOTH", "0.03"))
DIP_EDGE = float(os.environ.get("DIP_EDGE", "0.08"))
SHADOW_THRESHOLD = float(os.environ.get("SHADOW_THRESHOLD", "0.025"))

# ── Sequence screening (clouds over a night) ────────────────────────────────
# Frames of one target/filter/rig/exposure are split into sessions at gaps
# longer than SEQ_GAP_HOURS and compared, in capture order, with a rolling
# baseline of the last SEQ_BASELINE_FRAMES good frames. Flagged frames never
# enter the baseline, so a slow cloud bank can't drag it down; the session
# median is a second reference for the same reason. After 2×SEQ_BASELINE_FRAMES
# flagged frames in a row the baseline re-seeds (refocus, meridian flip).
SEQ_GAP_HOURS = float(os.environ.get("SEQ_GAP_HOURS", "3"))
SEQ_BASELINE_FRAMES = int(os.environ.get("SEQ_BASELINE_FRAMES", "5"))
SEQ_STAR_DROP = float(os.environ.get("SEQ_STAR_DROP", "0.3"))      # stars ≥30% below baseline
SEQ_HFR_RISE = float(os.environ.get("SEQ_HFR_RISE", "0.25"))       # HFR ≥25% above baseline
SEQ_EMPTY_RISE = float(os.environ.get("SEQ_EMPTY_RISE", "0.15"))   # +15 points of empty cells
SEQ_SPREAD_RISE = float(os.environ.get("SEQ_SPREAD_RISE", "1.0"))  # bg_spread doubled (stray light)
SEQ_FLUX_DROP = float(os.environ.get("SEQ_FLUX_DROP", "0.3"))      # bright-star flux ≥30% down (thin veil)
SEQ_DIP_RISE = float(os.environ.get("SEQ_DIP_RISE", "0.015"))      # background dip 1.5 points deeper (frost/dew)

# ── Sensor temperature ───────────────────────────────────────────────────────
# Frames whose CCD-TEMP is more than SETPOINT_TOLERANCE_C away from SET-TEMP are
# `off_setpoint` in selector expressions: the sensor was still cooling down, the
# cooler couldn't hold the setpoint on a warm night, or the setpoint was being
# changed — darks won't match them.
SETPOINT_TOLERANCE_C = float(os.environ.get("SETPOINT_TOLERANCE_C", "1.0"))

# ── PSF fit ──────────────────────────────────────────────────────────────────
# A circular Moffat profile is fitted to the PSF_FIT_STARS brightest clean stars
# (Levenberg–Marquardt, numpy only). Its FWHM follows the real profile rather
# than SEP's isophotal moments (median_fwhm); beta describes the wings (≈2–4
# for real seeing, large = Gaussian-like). Needs PSF_MIN_FITS good fits.
PSF_FIT_STARS = int(os.environ.get("PSF_FIT_STARS", "40"))
PSF_MIN_FITS = int(os.environ.get("PSF_MIN_FITS", "5"))

# ── Satellite / aircraft streaks ─────────────────────────────────────────────
# Found on a binned copy (long side ≈ 1600 px unless STREAK_BIN is set): sources
# at STREAK_THRESH σ at least STREAK_MIN_LENGTH of the frame diagonal long and
# STREAK_MIN_RATIO times longer than wide. Stars, trailed stars and curved
# nebula filaments fail the length or straightness test.
STREAK_BIN = int(os.environ.get("STREAK_BIN", "0"))
STREAK_THRESH = float(os.environ.get("STREAK_THRESH", "4.0"))
STREAK_MIN_LENGTH = float(os.environ.get("STREAK_MIN_LENGTH", "0.08"))
STREAK_MIN_RATIO = float(os.environ.get("STREAK_MIN_RATIO", "12"))

# ── Calibration coverage ─────────────────────────────────────────────────────
CAL_TEMP_TOLERANCE = float(os.environ.get("CAL_TEMP_TOLERANCE", "2"))   # °C, darks vs lights
CAL_FLAT_MAX_DAYS = float(os.environ.get("CAL_FLAT_MAX_DAYS", "30"))    # nearest flats older = stale

# ── Logging / diagnostics ────────────────────────────────────────────────────
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
SLOW_QUERY_MS = int(os.environ.get("SLOW_QUERY_MS", "300"))
SLOW_REQUEST_MS = int(os.environ.get("SLOW_REQUEST_MS", "1500"))
# SQLite busy timeout (ms) so concurrent writers from multiple containers retry
# instead of erroring while another holds the write lock.
SQLITE_BUSY_TIMEOUT_MS = int(os.environ.get("SQLITE_BUSY_TIMEOUT_MS", "10000"))
