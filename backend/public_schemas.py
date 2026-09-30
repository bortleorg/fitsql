"""
Response schemas for the public read-only API (/api/v1).

Frames are grouped into nested objects (file, capture, plate_solve, quality, ...)
so integrators can request exactly the parts they need with `fields=`. Every
field is optional in the schema because `fields` may omit it; when a field *is*
returned, `null` means "unknown / not measured".
"""
from typing import Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field


# ── Frames ────────────────────────────────────────────────────────────────────
class FileInfo(BaseModel):
    """Where the frame lives on disk and when it was indexed."""
    path: Optional[str] = Field(None, description="Absolute path as seen by the catalog server (e.g. the container path).",
                                examples=["/mnt/astro/M31/2026-05-13/LIGHT_L_300s_0001.fits"])
    host_path: Optional[str] = Field(None, description="`path` rewritten with the configured export path map "
                                     "(e.g. the path PixInsight sees). Equals `path` when no map is configured.",
                                     examples=["P:\\astro\\M31\\2026-05-13\\LIGHT_L_300s_0001.fits"])
    filename: Optional[str] = Field(None, examples=["LIGHT_L_300s_0001.fits"])
    format: Optional[Literal["fits", "xisf"]] = Field(None, description="File format, from the extension: `fits` "
                                                      "(.fit, .fits, .fts) or `xisf` (PixInsight / N.I.N.A.).",
                                                      examples=["fits"])
    size_bytes: Optional[int] = Field(None, description="File size in bytes.", examples=[124588800])
    modified_at: Optional[str] = Field(None, description="Filesystem modification time at index time (ISO 8601, UTC).",
                                       examples=["2026-05-13T04:05:12Z"])
    indexed_at: Optional[str] = Field(None, description="When the catalog last (re)indexed this file (ISO 8601, UTC). "
                                      "Use with `indexed_since` for incremental sync.", examples=["2026-05-14T10:00:00Z"])
    watched_path_id: Optional[int] = Field(None, description="Id of the watched library root this file was found under.")


class CaptureInfo(BaseModel):
    """Acquisition metadata, normalized from the FITS header."""
    object: Optional[str] = Field(None, description="OBJECT header.", examples=["M 31"])
    image_type: Optional[str] = Field(None, description="Normalized IMAGETYP.",
                                      examples=["Light Frame", "Dark Frame", "Flat Frame", "Bias Frame"])
    date_obs: Optional[str] = Field(None, description="Capture start time from DATE-OBS (ISO 8601, UTC, as written by the capture software).",
                                    examples=["2026-05-13T04:00:00.1234567"])
    exposure_s: Optional[float] = Field(None, description="EXPTIME, seconds.", examples=[300.0])
    filter: Optional[str] = Field(None, description="Normalized FILTER.", examples=["L", "Ha", "OIII"])
    telescope: Optional[str] = Field(None, description="TELESCOP.", examples=["GT71"])
    camera: Optional[str] = Field(None, description="INSTRUME.", examples=["ZWO ASI6200MM Pro"])
    focal_length_mm: Optional[float] = Field(None, description="FOCALLEN, millimetres.", examples=[336.0])
    gain: Optional[float] = Field(None, examples=[100.0])
    offset: Optional[int] = Field(None, examples=[50])
    sensor_temp_c: Optional[float] = Field(None, description="CCD-TEMP, °C.", examples=[-10.0])
    sensor_setpoint_c: Optional[float] = Field(None, description="SET-TEMP — the cooler setpoint, °C.", examples=[-10.0])
    binning_x: Optional[int] = Field(None, examples=[1])
    binning_y: Optional[int] = Field(None, examples=[1])
    width_px: Optional[int] = Field(None, description="NAXIS1.", examples=[9576])
    height_px: Optional[int] = Field(None, description="NAXIS2.", examples=[6388])
    bayer_pattern: Optional[str] = Field(None, description="BAYERPAT; null for mono sensors.", examples=["RGGB"])
    color: Optional[Literal["osc", "mono"]] = Field(None, description="`osc` when a Bayer pattern is present, else `mono`.")


class PointingInfo(BaseModel):
    """Where the mount/capture software said it was pointing (header RA/Dec)."""
    ra_deg: Optional[float] = Field(None, description="Header RA, degrees (J2000).", examples=[10.6847])
    dec_deg: Optional[float] = Field(None, description="Header Dec, degrees (J2000).", examples=[41.2690])


class PlateSolveInfo(BaseModel):
    """World coordinate solution (from header WCS or ASTAP)."""
    solved: Optional[bool] = Field(None, description="True when a WCS solution exists.")
    ra_deg: Optional[float] = Field(None, description="Solved field-centre RA, degrees.", examples=[10.6712])
    dec_deg: Optional[float] = Field(None, description="Solved field-centre Dec, degrees.", examples=[41.2801])
    pixel_scale_arcsec: Optional[float] = Field(None, description="Solved image scale, arcsec/pixel.", examples=[2.31])
    rotation_deg: Optional[float] = Field(None, description="Field rotation / position angle, degrees.", examples=[87.4])
    fov_width_deg: Optional[float] = Field(None, description="Field of view width, degrees.", examples=[6.14])
    fov_height_deg: Optional[float] = Field(None, description="Field of view height, degrees.", examples=[4.10])
    offset_from_pointing_arcmin: Optional[float] = Field(
        None, description="Separation between header pointing and the plate solve, arcminutes. Large values mean the "
                          "capture software's coordinates disagree with the actual field.", examples=[1.8])


class PositionInfo(BaseModel):
    """Best-available position and geometry: plate solve when present, else header/derived."""
    ra_deg: Optional[float] = Field(None, examples=[10.6712])
    dec_deg: Optional[float] = Field(None, examples=[41.2801])
    source: Optional[Literal["plate_solve", "header"]] = Field(None, description="Which source `ra_deg`/`dec_deg` came from.")
    pixel_scale_arcsec: Optional[float] = Field(None, description="Solved scale, else header-derived scale.", examples=[2.31])
    fov_width_deg: Optional[float] = Field(None, description="Solved FOV, else width_px × scale.", examples=[6.14])
    fov_height_deg: Optional[float] = Field(None, examples=[4.10])


class QualityInfo(BaseModel):
    """Star-based frame quality metrics (SEP source extraction).

    Shape metrics use the brightest *clean* stars (saturated, edge-truncated and blended sources are left out).
    Raw one-shot-colour frames (`capture.color = osc`) are measured on 2×2 CFA superpixels and reported in sensor
    pixels; their eccentricity and the moment-based FWHM of small stars read somewhat high compared with mono
    sensors, so prefer `hfr_px` / `psf_fwhm_px` when comparing OSC with mono frames."""
    star_count: Optional[int] = Field(None, examples=[1843])
    hfr_px: Optional[float] = Field(None, description="Median half-flux radius, pixels (focus).", examples=[2.21])
    fwhm_px: Optional[float] = Field(None, description="Median FWHM, pixels (seeing/focus).", examples=[3.05])
    fwhm_arcsec: Optional[float] = Field(None, description="Median FWHM, arcseconds (when scale is known).", examples=[7.04])
    eccentricity: Optional[float] = Field(None, description="Median star eccentricity, 0 = round (guiding/tilt).", examples=[0.42])
    trail_score: Optional[float] = Field(None, description="0–1 share of the brightest stars elongated in the same direction "
                                         "(tracking failure / mount slip). Frames above the server threshold are `trailed`; "
                                         "sub-second exposures score 0.", examples=[0.01])
    trailed: Optional[bool] = Field(None, description="`trail_score` above the server threshold (the `trailed` selector "
                                    "variable). Null until the frame is analyzed.", examples=[False])
    empty_cells: Optional[float] = Field(None, description="0–1 share of the grid (8×6 cells by default) covered by the largest "
                                         "connected patch of almost starless cells — tree line, dome, cloud bank. Null when "
                                         "the field is too sparse to judge. Frames above the server threshold are `occluded`.",
                                         examples=[0.0])
    occluded: Optional[bool] = Field(None, description="`empty_cells` above the server threshold (the `occluded` selector "
                                     "variable). Null when `empty_cells` is unknown.", examples=[False])
    background_spread_sigma: Optional[float] = Field(None, description="Cell-to-cell background spread (p90 − p10), in units of "
                                                     "background RMS — gradients, stray light.", examples=[4.2])
    background_dip: Optional[float] = Field(None, description="Deepest soft dark patch left in the star-clipped background "
                                            "after a smooth vignetting / gradient fit, as a fraction of the background "
                                            "(0.05 = 5% darker) — frost, dew or droplets on the sensor window, a filter or a "
                                            "corrector. Raw frames include the bias pedestal, so a shadow reads shallower on "
                                            "them than on calibrated frames, and dark nebulae read as dips too: compare "
                                            "within a session (`sequence.dip_rise`). Frames above the server threshold are "
                                            "`shadow`.", examples=[0.008])
    shadow: Optional[bool] = Field(None, description="`background_dip` above the server threshold (the `shadow` selector "
                                   "variable). Null until the frame is analyzed.", examples=[False])
    psf_fwhm_px: Optional[float] = Field(None, description="Median FWHM of a circular Moffat fit to the brightest clean stars, "
                                         "pixels.", examples=[3.2])
    psf_fwhm_arcsec: Optional[float] = Field(None, description="`psf_fwhm_px` × the effective pixel scale (plate-solved "
                                             "scale when present, else header-derived), arcseconds — the `psf_fwhm_arcsec` "
                                             "selector variable.", examples=[7.4])
    psf_beta: Optional[float] = Field(None, description="Median Moffat beta — the PSF wings (≈2–4 typical, large = "
                                      "Gaussian-like).", examples=[3.1])
    star_flux_adu: Optional[float] = Field(None, description="Median flux of the brighter stars (ranks 20–200), ADU. A "
                                           "transparency proxy — compare within one target/filter/exposure session.",
                                           examples=[48210.0])
    streak_count: Optional[int] = Field(None, description="Satellite / aircraft trails detected (long, thin, straight "
                                        "features).", examples=[0])
    satellite: Optional[bool] = Field(None, description="At least one streak detected (the `satellite` selector variable). "
                                      "Null until the frame is analyzed.", examples=[False])
    background_adu: Optional[float] = Field(None, description="Median sky background, ADU (light pollution / clouds).", examples=[812.0])
    pixel_scale_arcsec: Optional[float] = Field(None, description="Header-derived scale (pixel size & focal length), arcsec/pixel.", examples=[2.32])
    score: Optional[float] = Field(None, description="0–100 heuristic blend of the metrics above; higher is better.", examples=[78.5])


class ConditionsInfo(BaseModel):
    """Observing geometry at capture time (computed from DATE-OBS, position and site)."""
    altitude_deg: Optional[float] = Field(None, examples=[62.3])
    azimuth_deg: Optional[float] = Field(None, examples=[71.9])
    airmass: Optional[float] = Field(None, examples=[1.13])
    moon_separation_deg: Optional[float] = Field(None, examples=[98.2])
    moon_illumination: Optional[float] = Field(None, description="Illuminated fraction, 0–1.", examples=[0.35])
    moon_altitude_deg: Optional[float] = Field(None, description="Negative = below the horizon.", examples=[-12.0])


class StatusInfo(BaseModel):
    rejected: Optional[bool] = Field(None, description="Manually rejected by the user (excluded from exports).")
    grade: Optional[str] = Field(None, description="User grade: `accepted`, `rejected`, or null when unmarked.",
                                 examples=["accepted"])
    index_error: Optional[str] = Field(None, description="Why indexing failed; only set on frames listed with `indexing=failed|all`.")


class ThumbnailInfo(BaseModel):
    available: Optional[bool] = None
    url: Optional[str] = Field(None, description="Absolute URL of the JPEG preview (auto-stretched, ≤512 px). "
                               "Requires the same API key as the rest of the API.",
                               examples=["https://catalog.example/api/v1/frames/123/thumbnail"])
    content_type: Optional[str] = Field(None, examples=["image/jpeg"])
    width_px: Optional[int] = Field(None, examples=[512])
    height_px: Optional[int] = Field(None, examples=[342])


class FrameLinks(BaseModel):
    self: Optional[str] = Field(None, examples=["https://catalog.example/api/v1/frames/123"])
    preview: Optional[str] = Field(None, description="Large stretched JPEG for zooming (rendered on first request; needs "
                                   "the source file online).", examples=["https://catalog.example/api/v1/frames/123/preview"])
    diagnostics: Optional[str] = Field(None, description="Star and grid diagnostics behind the quality metrics (404 until "
                                       "the frame is analyzed).",
                                       examples=["https://catalog.example/api/v1/frames/123/diagnostics"])


class SequenceInfo(BaseModel):
    """How the frame compares with the rest of its imaging session — frames of the same image type, target, filter,
    camera, telescope and exposure, captured without a gap longer than the server's session gap (3 h by default).
    Each frame is compared, in capture order, with a rolling baseline of recent unflagged frames and with the session
    median; a passing cloud, thin veil, dew or stray light shows up as a sudden deviation. Computed over the whole
    session regardless of other request filters (in `where`, the sequence variables use the filtered frames instead)."""
    judged: Optional[bool] = Field(None, description="False when the frame can't be judged: no capture time, not "
                                   "analyzed, or fewer than 3 analyzed frames in its session.", examples=[True])
    anomaly: Optional[bool] = Field(None, description="Any of the deviations below past its server threshold (the "
                                    "`seq_anomaly` selector variable). Null when not judged.", examples=[False])
    star_drop: Optional[float] = Field(None, description="Fraction of stars lost vs the session reference (0.4 = 40% fewer).",
                                       examples=[0.02])
    hfr_rise: Optional[float] = Field(None, description="Fractional HFR increase vs the reference (0.3 = 30% softer).",
                                      examples=[-0.01])
    empty_rise: Optional[float] = Field(None, description="Increase of the empty-patch share of the grid (0.2 = +20 points).",
                                        examples=[0.0])
    spread_rise: Optional[float] = Field(None, description="Fractional increase of the background spread (stray light, "
                                         "gradients).", examples=[0.05])
    dip_rise: Optional[float] = Field(None, description="Increase of `quality.background_dip` vs the reference (0.03 = 3 "
                                      "points deeper) — frost or dew forming on the optics. Dark nebulae cancel out here, "
                                      "unlike in the frame's own dip.", examples=[0.0])
    flux_drop: Optional[float] = Field(None, description="Fraction of bright-star flux lost — a thin veil that leaves "
                                       "star counts intact.", examples=[0.03])
    transparency: Optional[float] = Field(None, description="1 − flux_drop: bright-star flux relative to the session "
                                          "reference, 1 = as clear as the session gets.", examples=[0.97])


HeaderValue = Union[bool, int, float, str, None]


class Frame(BaseModel):
    """One indexed FITS frame. Groups are returned according to `fields` / `expand`."""
    id: int = Field(..., description="Stable frame id. Always returned.", examples=[123])
    file: Optional[FileInfo] = None
    capture: Optional[CaptureInfo] = None
    pointing: Optional[PointingInfo] = None
    plate_solve: Optional[PlateSolveInfo] = None
    position: Optional[PositionInfo] = None
    quality: Optional[QualityInfo] = None
    conditions: Optional[ConditionsInfo] = None
    status: Optional[StatusInfo] = None
    thumbnail: Optional[ThumbnailInfo] = None
    links: Optional[FrameLinks] = None
    sequence: Optional[SequenceInfo] = Field(
        None, description="Session screening (clouds, veils, stray light). Only returned with `expand=sequence` or "
                          "`fields=sequence`.")
    header: Optional[Dict[str, HeaderValue]] = Field(
        None, description="Every FITS header card of the primary HDU (keyword → value). Heavy; only returned with "
                          "`expand=header` or `fields=header`.",
        examples=[{"SIMPLE": True, "BITPIX": 16, "EXPTIME": 300.0, "FILTER": "L", "DATE-OBS": "2026-05-13T04:00:00.123"}])


# Group name -> model, in response order. `header` is a free-form dict (no model).
FRAME_GROUPS = {
    "file": FileInfo, "capture": CaptureInfo, "pointing": PointingInfo, "plate_solve": PlateSolveInfo,
    "position": PositionInfo, "quality": QualityInfo, "conditions": ConditionsInfo, "status": StatusInfo,
    "thumbnail": ThumbnailInfo, "links": FrameLinks,
}
# Opt-in groups (costly to build): name -> model, None for the free-form header
HEAVY_GROUPS = {"header": None, "sequence": SequenceInfo}


class PageLinks(BaseModel):
    self: str
    next: Optional[str] = None
    prev: Optional[str] = None


class FramePage(BaseModel):
    items: List[Frame]
    total: int = Field(..., description="Frames matching all filters (including `where`).")
    page: int
    per_page: int
    pages: int
    links: PageLinks


class FieldDescriptor(BaseModel):
    path: str = Field(..., description="Dot path usable in `fields` (and `sort` when sortable).", examples=["quality.hfr_px"])
    type: str = Field(..., examples=["number"])
    description: Optional[str] = None
    sortable: bool = False
    default: bool = Field(..., description="Returned when `fields` is omitted.")


class FieldCatalog(BaseModel):
    groups: List[str]
    fields: List[FieldDescriptor]


# ── Aggregates ────────────────────────────────────────────────────────────────
class FilterIntegration(BaseModel):
    filter: str = Field(..., description="Filter name; `—` when the header had none.", examples=["Ha"])
    frames: int
    integration_sec: float
    avg_hfr: Optional[float] = None
    avg_eccentricity: Optional[float] = None
    avg_quality: Optional[float] = None
    avg_altitude: Optional[float] = None


class Rig(BaseModel):
    telescop: Optional[str] = None
    instrume: Optional[str] = None
    frames: int


class TargetSummary(BaseModel):
    """Light-frame rollup for one OBJECT header value."""
    object: str = Field(..., examples=["M 31"])
    frames: int
    integration_sec: float
    nights: int
    first_night: Optional[str] = Field(None, description="YYYY-MM-DD (UTC date of DATE-OBS).")
    last_night: Optional[str] = None
    avg_quality: Optional[float] = None
    avg_hfr: Optional[float] = None
    rejected: int
    filters: List[FilterIntegration]
    rigs: List[Rig]


class TargetList(BaseModel):
    targets: List[TargetSummary]
    total_integration_sec: float
    target_count: int


class Night(BaseModel):
    night: str = Field(..., description="YYYY-MM-DD (UTC date of DATE-OBS), or `Unknown`.")
    frames: int
    integration_sec: float
    telescop: Optional[str] = None
    instrume: Optional[str] = None
    moon_illum: Optional[float] = None
    filters: List[FilterIntegration]


class TargetDetail(BaseModel):
    object: str
    frames: int
    integration_sec: float
    night_count: int
    nights: List[Night]
    filters: List[FilterIntegration]


class UserTarget(BaseModel):
    """A user-defined sky position; frames match when their field of view covers it."""
    id: int
    name: str
    ra: float = Field(..., description="Degrees.")
    dec: float = Field(..., description="Degrees.")
    radius_deg: Optional[float] = Field(None, description="Also match frames whose centre is within this radius.")
    notes: Optional[str] = None
    frames: Optional[int] = None
    integration_sec: Optional[float] = None
    nights: Optional[int] = None
    first_night: Optional[str] = None
    last_night: Optional[str] = None
    avg_quality: Optional[float] = None
    avg_hfr: Optional[float] = None
    rejected: Optional[int] = None
    filters: Optional[List[FilterIntegration]] = None
    rigs: Optional[List[Rig]] = None


class UserTargetList(BaseModel):
    targets: List[UserTarget]
    total_integration_sec: float
    target_count: int


class UserTargetDetail(UserTarget):
    night_count: int
    nights: List[Night]


class ImagingSession(BaseModel):
    """Light frames of one object on one night."""
    object: str
    night: str
    telescop: Optional[str] = None
    instrume: Optional[str] = None
    frames: int
    integration_sec: float
    filters: List[FilterIntegration]


class SessionList(BaseModel):
    sessions: List[ImagingSession]
    total_integration_sec: float
    session_count: int


class CatalogSummary(BaseModel):
    total: int = Field(..., description="Indexed frames (excluding failed files).")
    failed: int = Field(..., description="Files that could not be indexed.")
    by_imagetyp: Dict[str, int]
    by_object: Dict[str, int] = Field(..., description="Top 20 objects by frame count.")
    by_filter: Dict[str, int]
    distinct: Dict[str, List[str]] = Field(..., description="Distinct `imagetypes`, `filters`, `telescopes`, `instruments` "
                                           "— handy for building filter dropdowns.")


class FrameDiagnostics(BaseModel):
    """What the analysis saw: per-star classification and the screening grid, in original frame pixels (raw OSC
    frames are mapped back from superpixels). Fields may be added in later analysis versions."""
    model_config = {"extra": "allow"}

    v: int = Field(..., description="Diagnostics format version.", examples=[1])
    width: Optional[int] = Field(None, description="Frame width, pixels.", examples=[9576])
    height: Optional[int] = Field(None, description="Frame height, pixels.", examples=[6388])
    grid: Optional[List[int]] = Field(None, description="Screening grid size as [columns, rows].", examples=[[8, 6]])
    cell_stars: Optional[List[int]] = Field(None, description="Detections per grid cell, row-major from the top-left.")
    empty_below: Optional[float] = Field(None, description="A cell holding fewer stars than this counts as empty.",
                                         examples=[42.4])
    judged: Optional[bool] = Field(None, description="False when the field is too sparse to judge empty cells.")
    empty_cluster: Optional[List[int]] = Field(None, description="Cell indices of the largest connected empty patch — "
                                               "what `quality.empty_cells` measures.")
    cell_bg_sigma: Optional[List[float]] = Field(None, description="Per-cell background offset from the frame median, in "
                                                 "units of background RMS (row-major).")
    stars: Optional[List[List[float]]] = Field(
        None, description="`[x, y, a, b, theta_deg, code, used]` for the measured sample plus the brightest stars left "
                          "out. `a`/`b` are the profile semi-axes (px); `code`: 0 clean, 1 saturated, 2 edge / truncated, "
                          "3 blended; `used` = 1 when the star is in the shape-metric sample.",
        examples=[[[1204.5, 873.1, 1.62, 1.41, 38, 0, 1]]])
    streaks: Optional[List[List[float]]] = Field(None, description="Detected satellite / aircraft trails as "
                                                 "`[x1, y1, x2, y2]`.", examples=[[[120.0, 80.0, 2310.0, 1490.0]]])
    dip: Optional[dict] = Field(None, description="The deepest background dip behind `quality.background_dip`: `depth` "
                                "(fraction of the background), the deepest point `x`, `y` and `box` = `[x0, y0, x1, y1]` "
                                "around the region at least half as deep.",
                                examples=[{"depth": 0.045, "x": 4653, "y": 2979, "box": [3600, 2412, 5724, 3492]}])
    sample: Optional[int] = Field(None, description="Stars in the shape-metric sample.", examples=[200])
    saturated: Optional[int] = Field(None, description="Detections classed as saturated.", examples=[37])
    tiles: Optional[int] = Field(None, description="Extraction tiles used (large frames are extracted in overlapping "
                                 "tiles).", examples=[9])


class CalibrationSetup(BaseModel):
    """One light setup and the calibration frames in the catalog that match it."""
    camera: str = Field(..., examples=["ZWO ASI6200MM Pro"])
    binning: int = Field(..., examples=[1])
    gain: Optional[float] = Field(None, examples=[100.0])
    offset: Optional[float] = Field(None, examples=[50.0])
    exposure_s: Optional[float] = Field(None, examples=[300.0])
    filter: Optional[str] = Field(None, examples=["Ha"])
    sensor_temp_c: Optional[int] = Field(None, description="Light sensor temperature, nearest °C.", examples=[-10])
    frames: int
    integration_sec: float
    nights: int
    first_night: Optional[str] = Field(None, description="YYYY-MM-DD (UTC date of DATE-OBS).")
    last_night: Optional[str] = None
    objects: List[str] = Field(..., description="Up to three targets imaged with this setup, most frames first.")
    darks: int = Field(..., description="Darks with the same camera, binning, gain, offset and exposure, within the "
                       "temperature tolerance.")
    flats: int = Field(..., description="Flats with the same camera, binning and filter.")
    bias: int = Field(..., description="Bias frames with the same camera, binning, gain and offset.")
    flat_gap_days: Optional[int] = Field(None, description="Worst gap from a light night to the nearest flat night, days.")
    stale_flats: bool = Field(..., description="`flat_gap_days` beyond the server's limit.")
    missing: List[Literal["darks", "flats"]] = Field(..., description="Calibration frame types with no match at all.")


class CalibrationSummary(BaseModel):
    setups: int
    complete: int = Field(..., description="Setups with darks and fresh flats.")
    missing_darks: int
    missing_flats: int
    stale_flats: int
    uncalibrated_integration_sec: float = Field(..., description="Light integration in setups missing darks or flats.")


class CalibrationFrameCounts(BaseModel):
    darks: int
    flats: int
    bias: int


class CalibrationTolerances(BaseModel):
    temp_c: float = Field(..., description="Maximum dark vs light sensor temperature difference, °C.")
    flat_max_days: float = Field(..., description="Flats further than this from a light night are stale.")


class CalibrationReport(BaseModel):
    setups: List[CalibrationSetup] = Field(..., description="Setups with problems first, then by integration time.")
    summary: CalibrationSummary
    calibration_frames: CalibrationFrameCounts
    tolerances: CalibrationTolerances


class SavedQueryInfo(BaseModel):
    id: int
    name: str
    folder: str = Field(..., description="'/'-separated folder, e.g. `Mono/NGC 7000`.")
    query: dict = Field(..., description="Selector state: `expression` plus base filters (imagetyp, object, filter, ...).")
    updated_at: Optional[str] = None


class ApiError(BaseModel):
    code: str = Field(..., examples=["invalid_parameter"])
    message: str
    details: Optional[dict] = None


class ErrorResponse(BaseModel):
    error: ApiError
