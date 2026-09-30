from datetime import datetime
from sqlalchemy import (
    create_engine, Column, Integer, String, Float, DateTime, ForeignKey, Text, event
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship, deferred

from config import DATABASE_URL

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
)

@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_conn, connection_record):
    import config
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA cache_size=-32000")  # 32MB cache
    cursor.execute("PRAGMA synchronous=NORMAL")
    # Wait for the write lock instead of erroring — multiple worker containers
    # write to this DB concurrently (WAL serialises writers).
    cursor.execute(f"PRAGMA busy_timeout={config.SQLITE_BUSY_TIMEOUT_MS}")
    cursor.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


class WatchedPath(Base):
    __tablename__ = "watched_paths"

    id = Column(Integer, primary_key=True, index=True)
    path = Column(String, unique=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    files = relationship("FitsFile", back_populates="watched_path", cascade="all, delete-orphan")


class FitsFile(Base):
    __tablename__ = "fits_files"

    id = Column(Integer, primary_key=True, index=True)
    path = Column(String, unique=True, nullable=False, index=True)
    filename = Column(String, nullable=False)
    watched_path_id = Column(Integer, ForeignKey("watched_paths.id"), nullable=True)
    file_size = Column(Integer, nullable=True)
    indexed_at = Column(DateTime, default=datetime.utcnow)

    # Thumbnail & analysis
    thumbnail = Column(String, nullable=True)
    star_count = Column(Integer, nullable=True)

    # Frame quality metrics (computed from SEP source extraction)
    median_hfr = Column(Float, nullable=True)        # half-flux radius, px (focus)
    median_fwhm = Column(Float, nullable=True)       # FWHM, px (seeing/focus)
    fwhm_arcsec = Column(Float, nullable=True)        # FWHM in arcsec (if pixel scale known)
    eccentricity = Column(Float, nullable=True)       # star roundness 0=round (guiding)
    trail_score = Column(Float, nullable=True)        # 0-1 share of stars trailed the same way (tracking)
    background_median = Column(Float, nullable=True)  # sky background ADU (light pollution)
    pixel_scale = Column(Float, nullable=True)        # arcsec/px
    quality_score = Column(Float, nullable=True)      # 0-100 heuristic blend
    # Grid screening: share of the grid covered by the largest connected patch of
    # (almost) starless cells — tree line, dome, cloud bank — and the cell-to-cell
    # background spread in units of background RMS (gradients, stray light).
    empty_cells = Column(Float, nullable=True)
    bg_spread = Column(Float, nullable=True)
    # Deepest soft dark patch left in the background after a smooth fit, as a fraction
    # of the background: frost or dew on the sensor window, a filter or a corrector
    bg_dip = Column(Float, nullable=True)
    # Circular Moffat fit to the brightest clean stars: FWHM (px) and beta (wings)
    psf_fwhm = Column(Float, nullable=True)
    psf_beta = Column(Float, nullable=True)
    # Median flux of the brighter stars (ADU) — transparency proxy within a session
    star_flux = Column(Float, nullable=True)
    # Satellite / aircraft trails detected
    streaks = Column(Integer, nullable=True)
    # Star/grid diagnostics for the preview overlay (JSON). Deferred: only the
    # detail view needs it, never list or selector queries.
    diagnostics_json = deferred(Column(Text, nullable=True))

    # Observing geometry (computed from DATE-OBS + RA/Dec + site via astropy)
    altitude = Column(Float, nullable=True)           # target altitude, deg
    azimuth = Column(Float, nullable=True)            # target azimuth, deg
    airmass = Column(Float, nullable=True)
    moon_sep = Column(Float, nullable=True)           # target-moon separation, deg
    moon_illum = Column(Float, nullable=True)         # illuminated fraction 0-1
    moon_alt = Column(Float, nullable=True)           # moon altitude, deg

    # Plate solution (Tier 1 from header WCS, or Tier 2 from ASTAP)
    solved = Column(Integer, nullable=True)           # 1 if a WCS solution exists
    wcs_ra = Column(Float, nullable=True)             # solved field-center RA, deg
    wcs_dec = Column(Float, nullable=True)            # solved field-center Dec, deg
    wcs_scale = Column(Float, nullable=True)          # solved pixel scale, arcsec/px
    wcs_rotation = Column(Float, nullable=True)       # field rotation / position angle, deg
    fov_w = Column(Float, nullable=True)              # field-of-view width, deg
    fov_h = Column(Float, nullable=True)              # field-of-view height, deg
    # Separation between acquisition (header ra/dec) and solved (wcs_ra/dec), arcmin.
    # Large = the capture software's pointing disagrees with the plate solve.
    coord_sep_arcmin = Column(Float, nullable=True)

    # If indexing failed (corrupt/truncated file), the error message; row is a
    # tombstone so scans skip it instead of retrying every time.
    index_error = Column(String, nullable=True)
    # Bayer pattern (BAYERPAT); set => one-shot-colour (OSC), absent => mono
    bayer = Column(String, nullable=True)
    # Manual grade (user override): 1 = rejected, 0 = accepted, null = unmarked
    rejected = Column(Integer, nullable=True)

    # Change detection
    file_mtime = Column(Float, nullable=True)         # filesystem mtime at index time

    # FITS header fields
    object = Column(String, nullable=True, index=True)
    imagetyp = Column(String, nullable=True, index=True)
    date_obs = Column(String, nullable=True)
    exptime = Column(Float, nullable=True)
    focal_length = Column(Float, nullable=True)
    telescop = Column(String, nullable=True, index=True)
    instrume = Column(String, nullable=True, index=True)
    filter = Column(String, nullable=True, index=True)
    ra = Column(Float, nullable=True)
    dec = Column(Float, nullable=True)
    gain = Column(Float, nullable=True)
    offset = Column(Integer, nullable=True)
    ccd_temp = Column(Float, nullable=True)
    set_temp = Column(Float, nullable=True)           # cooler setpoint (SET-TEMP)
    xbinning = Column(Integer, nullable=True)
    ybinning = Column(Integer, nullable=True)
    naxis1 = Column(Integer, nullable=True)
    naxis2 = Column(Integer, nullable=True)

    # Full header as JSON
    header_json = Column(Text, nullable=True)

    watched_path = relationship("WatchedPath", back_populates="files")


class Target(Base):
    """
    A user-defined target (sky position). Frames are matched to it spatially —
    any frame whose field of view covers the position counts — so one image can
    belong to several targets (e.g. a wide field with NGC 7000 and the Pelican),
    independent of what the capture software wrote in OBJECT.
    """
    __tablename__ = "targets"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    ra = Column(Float, nullable=False)          # degrees
    dec = Column(Float, nullable=False)         # degrees
    radius_deg = Column(Float, nullable=True)   # optional size; None = point match
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class SavedQuery(Base):
    """A saved Selector query, organised by a '/'-separated folder path."""
    __tablename__ = "saved_queries"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    folder = Column(String, default="", index=True)   # e.g. "Mono/NGC 7000"
    query_json = Column(Text)                          # full Selector state
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)


class AppConfig(Base):
    """Simple key-value config store (default site lat/lon/elev, etc.)."""
    __tablename__ = "app_config"

    key = Column(String, primary_key=True)
    value = Column(String, nullable=True)


def get_config(db, key, default=None):
    row = db.query(AppConfig).filter(AppConfig.key == key).first()
    return row.value if row else default


def set_config(db, key, value):
    row = db.query(AppConfig).filter(AppConfig.key == key).first()
    if row:
        row.value = value
    else:
        db.add(AppConfig(key=key, value=value))
    db.commit()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# Columns added after the initial schema — name -> SQL type for ALTER TABLE.
# SQLite has no "ADD COLUMN IF NOT EXISTS", so we diff against PRAGMA table_info.
_ADDED_COLUMNS = {
    "median_hfr": "FLOAT",
    "median_fwhm": "FLOAT",
    "fwhm_arcsec": "FLOAT",
    "eccentricity": "FLOAT",
    "trail_score": "FLOAT",
    "background_median": "FLOAT",
    "pixel_scale": "FLOAT",
    "quality_score": "FLOAT",
    "empty_cells": "FLOAT",
    "bg_spread": "FLOAT",
    "psf_fwhm": "FLOAT",
    "psf_beta": "FLOAT",
    "star_flux": "FLOAT",
    "streaks": "INTEGER",
    "diagnostics_json": "TEXT",
    "file_mtime": "FLOAT",
    "altitude": "FLOAT",
    "azimuth": "FLOAT",
    "airmass": "FLOAT",
    "moon_sep": "FLOAT",
    "moon_illum": "FLOAT",
    "moon_alt": "FLOAT",
    "solved": "INTEGER",
    "wcs_ra": "FLOAT",
    "wcs_dec": "FLOAT",
    "wcs_scale": "FLOAT",
    "wcs_rotation": "FLOAT",
    "fov_w": "FLOAT",
    "fov_h": "FLOAT",
    "coord_sep_arcmin": "FLOAT",
    "bayer": "TEXT",
    "rejected": "INTEGER",
    "index_error": "TEXT",
    "bg_dip": "FLOAT",
    "set_temp": "FLOAT",
}


def _migrate_columns(conn) -> set:
    """Add the missing columns; returns the ones this call added."""
    from sqlalchemy import text
    existing = {row[1] for row in conn.execute(text("PRAGMA table_info(fits_files)"))}
    added = set()
    for col, sqltype in _ADDED_COLUMNS.items():
        if col not in existing:
            try:
                conn.execute(text(f"ALTER TABLE fits_files ADD COLUMN {col} {sqltype}"))
                added.add(col)
            except Exception:
                # Another container (web/worker) may add it concurrently on first boot
                pass
    return added


def _backfill_set_temp(conn):
    """The cooler setpoint only lived in header_json before it got a column: copy it
    out once, without re-reading any FITS file."""
    from sqlalchemy import text
    for key in ("SET-TEMP", "SET_TEMP", "SETTEMP"):
        try:
            conn.execute(text(
                "UPDATE fits_files SET set_temp = json_extract(header_json, :path) "
                "WHERE set_temp IS NULL AND header_json IS NOT NULL AND json_valid(header_json) "
                "AND json_type(header_json, :path) IN ('integer', 'real')"), {"path": f'$."{key}"'})
        except Exception:
            return      # SQLite without JSON1: new scans still fill the column


def create_tables():
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        from sqlalchemy import text
        if "set_temp" in _migrate_columns(conn):
            _backfill_set_temp(conn)
        # Indexes for hot list query + sortable quality columns.
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_fits_imagetyp_date ON fits_files (imagetyp, date_obs)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_fits_date_obs ON fits_files (date_obs)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_fits_hfr ON fits_files (median_hfr)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_fits_quality ON fits_files (quality_score)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_fits_session ON fits_files (object, filter, date_obs)"))
