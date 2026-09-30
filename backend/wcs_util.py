"""
Parse an astrometric WCS solution from a FITS header (Tier 1 plate-solve: free,
uses whatever the capture software already wrote).

Returns accurate field center, pixel scale, rotation and field-of-view, which
feed the selector's spatial queries (rotation-aware `contains`, true FOV).
"""
from __future__ import annotations

import logging
import warnings
import numpy as np

logger = logging.getLogger(__name__)


def parse_wcs(header, naxis1=None, naxis2=None) -> dict | None:
    """
    Extract a celestial WCS solution from a FITS header.
    Returns dict(solved, wcs_ra, wcs_dec, wcs_scale, wcs_rotation, fov_w, fov_h)
    or None if the header has no usable celestial WCS.
    """
    try:
        from astropy.wcs import WCS
        from astropy.wcs.utils import proj_plane_pixel_scales

        nx = naxis1 or header.get("NAXIS1")
        ny = naxis2 or header.get("NAXIS2")

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            w = WCS(header)
            if not w.has_celestial:
                logger.warning("WCS parse: header has no celestial WCS")
                return None
            w = w.celestial

            scales = proj_plane_pixel_scales(w)  # deg/px along each axis
            scale_x, scale_y = float(scales[0]), float(scales[1])
            wcs_scale = (scale_x + scale_y) / 2.0 * 3600.0  # arcsec/px

            if nx and ny:
                # Field center from the central pixel (1-based FITS convention)
                center = w.pixel_to_world((nx + 1) / 2.0, (ny + 1) / 2.0)
                fov_w = scale_x * nx
                fov_h = scale_y * ny
            else:
                # No image dims (e.g. a bare ASTAP .wcs) — use the reference point
                # (CRPIX/CRVAL), which ASTAP places at the image center.
                cx = float(header.get("CRPIX1", 1.0))
                cy = float(header.get("CRPIX2", 1.0))
                center = w.pixel_to_world(cx, cy)
                fov_w = fov_h = None
            ra = float(center.ra.deg)
            dec = float(center.dec.deg)

            # Rotation (position angle of +Y axis) from the CD/PC matrix
            cd = w.pixel_scale_matrix  # 2x2, deg/px
            rotation = float(np.degrees(np.arctan2(cd[0, 1], cd[1, 1])))

        if not (np.isfinite(ra) and np.isfinite(dec) and np.isfinite(wcs_scale)):
            return None
        if wcs_scale <= 0 or wcs_scale > 3600:
            return None

        return {
            "solved": 1,
            "wcs_ra": round(ra, 6),
            "wcs_dec": round(dec, 6),
            "wcs_scale": round(wcs_scale, 4),
            "wcs_rotation": round(rotation % 360.0, 3),
            "fov_w": round(fov_w, 5) if fov_w is not None else None,
            "fov_h": round(fov_h, 5) if fov_h is not None else None,
        }
    except Exception as e:
        logger.warning(f"WCS parse failed: {type(e).__name__}: {e}")
        return None
